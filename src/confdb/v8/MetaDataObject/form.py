"""Парсеры форм 5.x/9.x (слитый порт v8unpack MetaDataObject.Form, MIT)."""
import os
import re
from .form_elements26 import FormElements26
from .form_elements27 import FormElements27
from .form_elements4 import FormElements4
from .core import SimpleNameFolder
from .. import helper
from ..ext_exception import ExtException
import json


# --- MetaDataObject/Form/FormCore.py ---
OF = '0'  # обычные формы
UF = '1'  # управляемые формы


class FormCore(SimpleNameFolder):
    _obj_name = "Form"
    double_quotes = re.compile(r'("")')
    quotes = re.compile(r'(")')
    supported_form_versions = {
        # '0-2': FormElements2,
        # '0-23': FormElements26,# 801
        # '0-25': FormElements26,# 801
        '0-26': FormElements26,  # 801
        '0-27': FormElements27,  # OF
        '1': FormElements4,  # UF
        # '1-4-50': FormElements4,
        # '1-4-49': FormElements4,
        # '1-3-49': FormElements4
    }

    def __init__(self, *, meta_obj_class=None, obj_version=None, options=None):
        super().__init__(meta_obj_class=meta_obj_class, obj_version=obj_version, options=options)
        # self.form = []
        self.elements_tree = []
        self.elements_data = {}
        self.props_index = {}
        self.props = []
        self.params = []
        self.commands = []
        self._form = None

    @property
    def form(self):
        try:
            return self.header['form']
        except KeyError:
            self.header['form'] = []
        return self.header['form']

    @form.setter
    def form(self, value):
        self.header['form'] = value

    @classmethod
    def get_form_root(cls, header_data):
        return header_data[0][1][1]

    def get_decode_header(self, header_data):
        try:
            _form_root_getter = getattr(self.meta_obj_class, 'get_form_root')
        except Exception as err:
            _form_root_getter = self.get_form_root
        return _form_root_getter(header_data)[1][1]

    def decode_object(self, src_dir, file_name, dest_dir, dest_path, version, header_data):
        # self.decode_header(header_data)
        self.set_write_decode_mode(dest_dir, dest_path)
        self.decode_data(src_dir, file_name)

    def decode_form0(self, src_dir):
        file_name = f'{self.header["uuid"]}.0'
        _code_dir = os.path.join(src_dir, file_name)
        if os.path.exists(_code_dir):
            if os.path.isdir(_code_dir):
                self.decode_form0_from_dir(_code_dir)
            else:
                self.decode_form0_from_file(_code_dir, file_name)
                pass

    def decode_form1(self, src_dir):
        try:
            form = helper.brace_file_read(src_dir, f'{self.header["uuid"]}.1')
        except FileNotFoundError:
            return
        self.form.append(form)

    def decode_form0_from_dir(self, src_dir):
        self.form.append(helper.brace_file_read(src_dir, 'form'))
        self.code['obj'], encoding = self.read_raw_code(src_dir, 'module', uncomment_directive=True)
        self.header['code_encoding_obj'] = encoding  # можно безболезненно поменять на utf-8-sig
        self.header[f'code_info_obj'] = 1

    def decode_form0_from_file(self, src_dir, file_name=None):
        if file_name is None:
            file_name = f'{self.header["uuid"]}.0'
        try:
            form = helper.brace_file_read(src_dir, file_name)
        except FileNotFoundError:
            self.form.append([])
            return
        try:
            _code = helper.str_decode(self.getset_form_code(form, 'Код в отдельном файле', self.header))
            if _code is not None:
                _code = self.double_quotes.sub(r'"', _code)
                self.code['obj'] = _code
                self.header['code_info_obj'] = 'Код в отдельном файле'
        except Exception as err:
            raise ExtException(parent=err, detail=self.header['uuid'])
        self.form.append(form)

    @classmethod
    def getset_form_code(cls, form, new_value=None, header=None):
        err_detail = f'{header["uuid"]} {header["name"]} ' \
                     f'опытным путем подобрано, если у Вас код не достается' \
                     f'обновитесь до последней версии, и если не поможет создайте issue с дампом'
        len_form_0 = len(form[0])
        if len_form_0 > 2 and form[0][0] in ['4', '3', '2']:
            code = form[0][2]
            if not isinstance(code, str):
                raise ExtException(
                    message='Not supported forms',
                    detail=err_detail,
                    dump=form
                )
            form[0][2] = new_value
            return code

        last_level = cls.get_last_level_array(form)
        if len_form_0 < 10 and (last_level[0] == '49' or last_level[0] == '4'):
            return ''

        if len(last_level) > 10 \
                and last_level[0] in ['22', '1'] \
                and last_level[-1] == '0' \
                and last_level[-2] == '0':
            code_index = -8
            code = last_level[code_index]
            if not isinstance(code, str):
                raise ExtException(
                    message='Not supported forms',
                    detail=err_detail,
                    dump=form
                )
            if new_value is not None:
                last_level[code_index] = new_value
            return code
        return ''

    def get_form_element_decoder(self):
        self.header['Версия элементов формы'] = ''
        if not self.form or not self.form[0]:
            return
        self.header['Версия элементов формы'] = self.header['Тип формы']
        try:
            return self.supported_form_versions[self.header['Версия элементов формы']]
        except KeyError:
            pass

        self.header['Версия элементов формы'] += f"-{self.form[0][0][0]}"
        try:
            return self.supported_form_versions[self.header['Версия элементов формы']]
        except KeyError:
            pass

        try:
            v2 = self.form[0][0][1][0]
            self.header['Версия элементов формы'] += f'-{v2}'
        except (KeyError, TypeError, AttributeError):
            pass

        try:
            return self.supported_form_versions[self.header['Версия элементов формы']]
        except KeyError:
            ver = self.header['Версия элементов формы']
            return

    def decode_includes(self, src_dir, dest_dir, dest_path, header_data):
        try:
            if self.header['Тип формы'] == OF:
                self.decode_form0(src_dir)
                self.decode_code(src_dir, uncomment_directive=True)
            else:
                self.decode_form0_from_file(src_dir)
            self.decode_form1(src_dir)

            form_element_decoder = self.get_form_element_decoder()
            if not form_element_decoder:
                return

            backup = json.dumps(self.form)
            try:
                form_element_decoder(self).decode(src_dir, dest_dir, dest_path, self.form[0][0])
            except Exception as err:
                self.form = json.loads(backup)
                pass  # todo если какие то елементы формы не разбираются, не прерываем
                # raise ExtException(parent=err, message='Ошибка при разборе формы')
        except Exception as err:
            raise ExtException(parent=err)

    def write_decode_object(self, dest_dir, dest_path, file_name):
        dest_full_path = os.path.join(dest_dir, dest_path)
        id_data = {
            'uuid': self.header.pop('uuid'),
            # 'name': self.header.pop('name'),
        }
        self.header['obj_version'] = self.obj_version
        helper.json_write(id_data, dest_full_path, f'{file_name}.id.json')
        helper.json_write(self.header, dest_full_path, f'{file_name}.json')
        self.write_decode_code(dest_full_path, file_name)

        helper.json_write(
            dict(
                params=self.params,
                props=self.props,
                commands=self.commands,
                tree=self.elements_tree,
                data=self.elements_data,
            ),
            self.new_dest_dir, f"{file_name}.elem.json")
        return []

    def decode_data(self, src_dir, uuid):
        _header_obj = self.meta_obj_class.get_form_root(self.header['header'])
        try:
            self.header['Тип формы'] = _header_obj[1][3]
        except IndexError:
            self.header['Тип формы'] = OF
        return _header_obj

    @classmethod
    def get_last_level_array(cls, data):
        while True:
            if isinstance(data[-1], list):
                return cls.get_last_level_array(data[-1])
            else:
                return data


# --- MetaDataObject/Form/Form5.py ---
class Form5(FormCore):
    pass


# --- MetaDataObject/Form/Form9.py ---
class Form9(FormCore):

    def __init__(self, *, meta_obj_class=None, obj_version=None, options=None):
        super().__init__(meta_obj_class=meta_obj_class, obj_version=obj_version, options=options)

        self.params = []
        self.commands = []


# --- MetaDataObject/Form/__init__.py ---
class Form(SimpleNameFolder):
    versions = {
        '5': Form5,
        '7': Form5,
        '9': Form9,
        '12': Form9,
        '13': Form9,
        '14': Form9,
    }
    # @classmethod
    # def read_header(cls, src_dir, src_file_name, data_id):
    #     return super().read_header(src_dir, f"{data_id['type']}{src_file_name}", data_id)

    @classmethod
    def get_form_root(cls, header_data):
        # return header_data[0][1][1]
        obj_version = header_data[0][1][0]
        if obj_version == '0':
            return header_data[0][1]
        elif obj_version == '1':
            return header_data[0][1][1]
        raise NotImplementedError()

    @classmethod
    def get_decode_header(cls, header_data):
        return cls.get_form_root(header_data)[1][1]

    @classmethod
    def get_version(cls, header_data, options):
        form_root = cls.get_form_root(header_data)
        form_version = form_root[1][0]
        return form_version


class Form1(Form):
    @classmethod
    def get_form_root(cls, header_data):
        return header_data[0][1]


class Form0(Form):
    @classmethod
    def get_form_root(cls, header_data):
        return header_data[0]
