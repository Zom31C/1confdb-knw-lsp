"""Корневые объекты: MetaObject, Configuration, ConfigurationExtension, ExternalDataProcessor (слитый порт v8unpack.MetaObject, MIT)."""
import os
import re
import shutil
from base64 import b64decode
from . import helper
from .ext_exception import ExtException
from .metadata_types import MetaDataTypes
from . import __version__
from .metadata_types import MetaDataGroup


# --- MetaObject/__init__.py ---
class MetaObject:
    ext_code = {'obj': 0}
    encrypted_types = ['text', 'image', 'bin']
    _unknown_binary = None
    _obj_info = None
    _obj_name = None

    re_meta_data_obj = re.compile('^[^.]+\.json$')
    directive_1c_uncomment = re.compile('(?P<n>\\n)(?P<d>#Область|#КонецОбласти|&НаСервере|&НаКлиенте)')
    directive_1c_comment = re.compile('(?P<n>\\n)(?P<c>// v8unpack )(?P<d>[#|&])')

    def __init__(self, *, meta_obj_class=None, obj_version='803', options=None):
        self.meta_obj_class = meta_obj_class
        self.header = {}
        self.uuid = None
        self.name = None
        self.container_uuid = None
        self.parent_container_uuid = None
        self.code = {}
        self.file_list = []
        self.obj_version = obj_version
        self.options = options

    @classmethod
    def get_container_uuid(cls, header_data):
        return None

    def get_options(self, name, default=None):
        try:
            return self.options[name]
        except (TypeError, KeyError):
            return default

    @classmethod
    def get_decode_header(cls, header_data):
        return header_data[0][1][1]

    def decode_header(self, header_data, *, id_in_separate_file=True):
        try:
            header = self.get_decode_header(header_data)
            helper.decode_header(self, header, id_in_separate_file=id_in_separate_file)
            self.uuid = self.header['uuid']
            self.name = self.header['name']
            self.header['header'] = header_data
            self.container_uuid = self.get_container_uuid(header_data)
        except Exception as err:
            raise ExtException(parent=err)

    def decode_includes(self, src_dir, dest_dir, dest_path, header_data):
        tasks = []
        includes = self.get_decode_includes(header_data)
        for include in includes:
            self.decode_include(src_dir, dest_dir, dest_path, tasks, include)
        return tasks

    def decode_include(self, src_dir, dest_dir, dest_path, tasks, include):
        auto_include = self.get_options('auto_include')
        try:
            count_include_types = int(include[2])
        except IndexError:
            raise ExtException(message='Include types not found', detail=self.__class__.__name__)
        for i in range(count_include_types):
            _metadata = include[i + 3]
            _count_obj = int(_metadata[1])
            _metadata_type_uuid = _metadata[0]
            try:
                metadata_type = MetaDataTypes(_metadata_type_uuid)
            except ValueError:
                if not _count_obj:
                    continue

                if not isinstance(_metadata[2], str):  # вложенный объект
                    continue
                msg = f'У {self.__class__.__name__} {self.header["name"]} неизвестный тип вложенных метаданных: {_metadata_type_uuid} лежит в файле {_metadata[2]}'
                print(msg)
                continue
                # raise Exception(msg)
            if not _count_obj:
                continue
            new_dest_path = os.path.join(dest_path, metadata_type.name)
            external_obj = False
            internal_obj = False
            for j in range(_count_obj):
                obj_data = _metadata[j + 2]
                if isinstance(obj_data, str):
                    if j == 0:
                        os.mkdir(os.path.join(dest_dir, new_dest_path))

                    tasks.append([metadata_type.name,
                                  [src_dir, obj_data, dest_dir, new_dest_path, self.container_uuid, self.options]])
                    external_obj = True
                elif isinstance(obj_data, list):
                    if not metadata_type:
                        continue
                    try:
                        handler = helper.get_class_metadata_object(metadata_type.name)
                    except Exception as err:
                        continue
                    if j == 0:
                        os.mkdir(os.path.join(dest_dir, new_dest_path))
                    obj_uuid = handler.decode_internal_include(self, obj_data, src_dir, dest_dir, new_dest_path,
                                                               self.options)
                    if not auto_include:
                        # заменяем данные на идентификатор
                        _metadata[j + 2] = obj_uuid
                    internal_obj = True
            if (external_obj or internal_obj) and auto_include:  # todo dynamic index
                include[i + 3] = metadata_type.name

    @classmethod
    def get_decode_includes(cls, header_data: list) -> list:
        raise NotImplementedError(f'Для метаданных {cls.__name__} не указана ссылка на includes')

    def get_class_name_without_version(self):
        return self.__class__.__name__

    def read_raw_code(self, src_dir, file_name, *, uncomment_directive=False):
        encoding = 'utf-8'
        try:
            code = helper.txt_read(src_dir, file_name, encoding=encoding)
            encoding = helper.detect_by_bom(os.path.join(src_dir, file_name), 'utf-8')
        except FileNotFoundError as err:
            code = None
        except UnicodeDecodeError:
            try:
                encoding = 'windows-1251'
                code = helper.txt_read(src_dir, file_name, encoding=encoding)
            except UnicodeDecodeError:
                encoding = 'bin'
                code = helper.bin_read(src_dir, file_name)

        if code and encoding != 'bin':
            # if self.options['version'] in ['801', '802'] or uncomment_directive:  # раскомментируем директивы
            if uncomment_directive:  # раскомментируем директивы
                code = self.directive_1c_comment.sub(r'\g<n>\g<d>', code)
        return code, encoding

    def write_raw_code(self, code, dest_dir, filename, *, encoding='uft-8', comment_directive=False):
        if code is not None:
            # if self.options['version'] in ['801', '802'] or comment_directive:  # комментируем директивы
            if comment_directive:  # комментируем директивы
                code = self.directive_1c_uncomment.sub(r'\g<n>// v8unpack \g<d>', code)
            helper.txt_write(code, dest_dir, filename, encoding=encoding)

    def decode_code(self, src_dir, *, uncomment_directive=False):
        for code_name in self.ext_code:
            if code_name in self.code:
                continue  # код был в файле с формой
            _obj_code_dir = f'{os.path.join(src_dir, self.header["uuid"])}.{self.ext_code[code_name]}'
            if not os.path.exists(_obj_code_dir):
                continue
            if os.path.isdir(_obj_code_dir):
                self.header[f'code_info_{code_name}'] = helper.brace_file_read(_obj_code_dir, 'info')
                try:
                    self.code[code_name], encoding = self.read_raw_code(_obj_code_dir, 'text',
                                                                        uncomment_directive=uncomment_directive)
                    self.header[f'code_encoding_{code_name}'] = encoding  # можно безболезненно поменять на utf-8-sig
                except FileNotFoundError as err:
                    # todo могут быть зашифрованные модули тогда файл будет # image.json - зашифрованный контент
                    not_encrypted = True
                    for encrypted_type in self.encrypted_types:
                        if os.path.isfile(os.path.join(_obj_code_dir, encrypted_type)):
                            self.code[code_name] = helper.bin_read(_obj_code_dir, encrypted_type)
                            self.header[f'code_encoding_{code_name}'] = encrypted_type
                            not_encrypted = False
                            break
                    if not_encrypted:
                        raise err from err
            else:
                code_file_name = f'{self.header["uuid"]}.{self.ext_code[code_name]}'
                self.code[code_name], encoding = self.read_raw_code(src_dir, code_file_name,
                                                                    uncomment_directive=uncomment_directive)

                self.header[f'code_info_{code_name}'] = 'file'
                self.header[f'code_encoding_{code_name}'] = encoding  # можно безболезненно поменять на utf-8-sig

    def write_decode_code(self, dest_dir, file_name):
        for code_name in self.code:
            if self.code[code_name]:
                encoding = self.header.get(f'code_encoding_{code_name}')
                if encoding in self.encrypted_types:
                    helper.bin_write(self.code[code_name], dest_dir, f'{file_name}.{code_name}.{encoding}')
                else:
                    helper.txt_write(self.code[code_name], dest_dir, f'{file_name}.{code_name}.bsl')

    def _decode_html_data(self, src_dir, dest_dir, dest_file_name, *, header_field='html', file_number=0,
                          extension='html'):
        try:
            file_name = f'{self.header["uuid"]}.{file_number}'
            file_size = os.path.getsize(os.path.join(src_dir, file_name))
            if file_size > 1000000:  # если файл больше мегабайте не разбираем
                shutil.copy2(
                    os.path.join(src_dir, file_name),
                    os.path.join(dest_dir, f'{dest_file_name}.bin')
                )
                return
            data = helper.brace_file_read(src_dir, file_name)
        except FileNotFoundError:
            return
        try:
            if data[0][3] and data[0][3][0]:
                bin_data = self._extract_b64_data(data[0][3])
                helper.bin_write(bin_data, dest_dir, f'{dest_file_name}.{extension}')
        except IndexError:
            pass
        self.header[header_field] = data

    def _extract_b64_data(self, raw_data):
        if raw_data[0].startswith('##base64:'):
            bin_data = b64decode(raw_data[0][9:])
            raw_data[0] = '##base64:'
        elif raw_data[0].startswith('#base64:'):
            bin_data = b64decode(raw_data[0][8:])
            raw_data[0] = '#base64:'
        elif raw_data[0].startswith('#data:'):
            bin_data = b64decode(raw_data[0][6:])
            raw_data[0] = '#data:'
        else:
            raise NotImplementedError('decode_html_data')
        return bin_data

    def _decode_info(self, src_dir, dest_dir, dest_file_name):
        if self._obj_info:
            for elem in self._obj_info:
                try:
                    data = helper.brace_file_read(src_dir, f'{self.header["uuid"]}.{self._obj_info[elem]}')
                    helper.json_write(data, dest_dir, f'{dest_file_name}.{self._obj_info[elem]}.json')

                except FileNotFoundError:
                    pass

    def _decode_unknown(self, src_dir, dest_dir, dest_file_name):
        if self._unknown_binary:
            for elem in self._unknown_binary:
                try:
                    shutil.copy2(
                        os.path.join(src_dir, f'{self.header["uuid"]}.{self._unknown_binary[elem]}'),
                        os.path.join(dest_dir, f'{dest_file_name}.{elem}.bin')
                    )
                except FileNotFoundError:
                    pass


# --- MetaObject/Configuration.py ---
class Configuration(MetaObject):
    ext_code = {
        'seance': 7,
        'app': 6,
        '802': 0,
        'con': 5
    }
    help_file_number = 3

    _images = {
        'Заставка': 2
    }

    _obj_info = {
        '4': '4',
        '9': '9',
        '8': '8',
        '10': '10',
        'a': 'a',
        'b': 'b',
        'c': 'c',
        'd': 'd',
        'e': 'e',
        'f': 'f',
    }

    def __init__(self, *, meta_obj_class=None, obj_version=None, options=None):
        super().__init__(meta_obj_class=meta_obj_class, obj_version=obj_version, options=options)
        self.counter = {}

    def decode(self, src_dir, dest_dir, *, version=None, **kwargs):
        self.header = {}
        root = helper.brace_file_read(src_dir, 'root')
        self.header['v8unpack'] = __version__
        self.header["file_uuid"] = root[0][1]
        self.header["root"] = root

        _header_data = helper.brace_file_read(src_dir, f'{self.header["file_uuid"]}')
        self.decode_header(_header_data, id_in_separate_file=False)

        file_name = self.get_class_name_without_version()

        self.header['version'] = helper.brace_file_read(src_dir, 'version')
        self.header['versions'] = helper.brace_file_read(src_dir, 'versions')

        if version is None:
            self.header['compatibility_version'] = self.header['header'][0][3][1][1][26]
        else:
            self.header['compatibility_version'] = version
            self.header['header'][0][3][1][1][26] = version

        self.decode_code(src_dir, uncomment_directive=self.obj_version in ['802', '801'])
        self._decode_html_data(src_dir, dest_dir, 'help', header_field='help', file_number=self.help_file_number)
        self._decode_images(src_dir, dest_dir)
        self._decode_info(src_dir, dest_dir, file_name)
        # self._decode_unknown(src_dir, dest_dir, file_name)

        tasks = self.decode_includes(src_dir, dest_dir, '', self.header['header'])
        self.header['obj_version'] = self.obj_version
        helper.json_write(self.header, dest_dir, f'{file_name}.json')
        self.write_decode_code(dest_dir, file_name)
        return tasks

    @classmethod
    def get_decode_header(cls, header):
        return header[0][3][1][1][1][1]

    def decode_includes(self, src_dir, dest_dir, dest_path, header_data):
        tasks = []
        index_includes_group = 2
        count_includes_group = int(header_data[0][index_includes_group])
        for index_group in range(count_includes_group):
            group = header_data[0][index_includes_group + index_group + 1]
            group_uuid = group[0]
            group_version = group[1][0]
            try:
                metadata_group = MetaDataGroup(group_uuid)
            except ValueError:
                raise ExtException(message='Неизвестная группа метаданных', detail=group_uuid)
            include = group[1][1] if group_version == '6' else group[1]
            self.decode_include(src_dir, dest_dir, dest_path, tasks, include)
        return tasks

    def _decode_images(self, src_dir, dest_dir):
        if self._images:
            for elem in self._images:
                try:
                    data = helper.brace_file_read(src_dir, f'{self.header["uuid"]}.{self._images[elem]}')
                except FileNotFoundError:
                    return
                try:
                    if data[0][2] and data[0][2][0] and data[0][2][0][0]:
                        bin_data = self._extract_b64_data(data[0][2][0])
                        helper.bin_write(bin_data, dest_dir, f'{elem}')
                except IndexError:
                    pass
                self.header[f'image_{elem}'] = data


# --- MetaObject/ExternalDataProcessor.py ---
class ExternalDataProcessor(MetaObject):

    def __init__(self, *, meta_obj_class=None, obj_version=None, options=None):
        super().__init__(meta_obj_class=meta_obj_class, obj_version=obj_version, options=options)
        self.data = None

    def decode(self, src_dir, dest_dir):
        self.header = {}
        self.data = {}
        root = helper.brace_file_read(src_dir, 'root')
        self.header['root'] = True
        self.header["file_uuid"] = root[0][1]
        _header_data = helper.brace_file_read(src_dir, f'{self.header["file_uuid"]}')
        self.decode_header(_header_data, id_in_separate_file=False)

        root = helper.brace_file_read(src_dir, 'root')
        self.header['v8unpack'] = __version__
        self.header['file_uuid'] = root[0][1]
        self.header['version'] = helper.brace_file_read(src_dir, 'version')
        self.header['copyinfo'] = helper.brace_file_read(src_dir, 'copyinfo')

        try:
            form1 = helper.brace_file_read(src_dir, f'{self.header["uuid"]}.1')
        except FileNotFoundError:
            form1 = None

        self.header['form1'] = form1

        self.decode_code(src_dir, uncomment_directive=self.obj_version in ['802', '801'])
        _file_name = self.get_class_name_without_version()
        self.container_uuid = self.get_container_uuid(self.header['header'])
        tasks = self.decode_includes(src_dir, dest_dir, '', self.header['header'])

        self.header['obj_version'] = self.obj_version
        helper.json_write(self.header, dest_dir, f'{_file_name}.json')
        self.write_decode_code(dest_dir, 'ExternalDataProcessor')

        return tasks

    @classmethod
    def get_container_uuid(cls, header_data):
        return header_data[0][3][1][1][1]

    @classmethod
    def get_decode_includes(cls, header_data):
        return [header_data[0][3][1]]

    @classmethod
    def get_decode_header(cls, header_data):
        return header_data[0][3][1][1][3][1]


# --- MetaObject/ConfigurationExtension.py ---
class ConfigurationExtension(Configuration):
    info = ['8', '9']
    ext_code = {
        'con': '5',
        'app': '6',
        'ssn': '7'
    }
    _obj_info = {
        'a': 'a',
    }

    def decode(self, src_dir, dest_dir, *, version=None, **kwargs):
        self.header = {}
        root = helper.brace_file_read(src_dir, 'configinfo')
        self.header['configinfo'] = root
        self.header['v8unpack'] = __version__
        self.header['file_uuid'] = root[1][1]
        self.header['version'] = root[0][1]
        self.header['copyinfo'] = root[1]
        self.header['header'] = helper.brace_file_read(src_dir, f'{self.header["file_uuid"]}')
        _form_header = self.get_decode_header(self.header['header'])
        helper.decode_header(self, _form_header, id_in_separate_file=False)
        product_version = self.header['header'][0][3][1][1][15]
        if version is None:
            self.header['compatibility_version'] = self.header['header'][0][3][1][1][43]
        else:
            self.header['compatibility_version'] = version
            self.header['header'][0][3][1][1][43] = version

        self.decode_code(src_dir)

        for i in self.info:  # хз что это
            file_name = f'{self.header["uuid"]}.{i}'
            if os.path.isdir(os.path.join(src_dir, file_name)):
                continue
            try:
                self.header[f'info{i}'] = helper.brace_file_read(src_dir, file_name)
            except FileNotFoundError:
                pass
        file_name = self.get_class_name_without_version()
        self._decode_info(src_dir, dest_dir, file_name)
        tasks = self.decode_includes(src_dir, dest_dir, '', self.header['header'])

        helper.txt_write(helper.str_decode(product_version), dest_dir, 'version.bin', encoding='utf-8')
        self.header['obj_version'] = self.obj_version
        helper.json_write(self.header, dest_dir, f'{self.get_class_name_without_version()}.json')
        self.write_decode_code(dest_dir, self.__class__.__name__)

        return tasks
