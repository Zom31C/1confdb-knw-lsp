"""Классы объектов метаданных конфигурации (слитый порт v8unpack.MetaDataObject, MIT)."""
from . import MetaDataObject
import os
import shutil
from base64 import b64decode
from enum import Enum
from .core import Simple
from .. import helper
from ..ext_exception import ExtException, ExtNotImplemented
from ..json_container_decoder import BigBase64
from .core import Container
from .core import IncludeSimple
from .form import Form0
from .form import Form1
from .core import SimpleNameFolder
from .core import FormContainer
from .form import Form9
from ..ext_exception import ExtException
from .core import SimpleWithInfo
from .form import Form


# --- MetaDataObject/Template.py ---
class TmplType(Enum):
    table = "0"
    base64 = "1"
    active_doc = "2"
    html = "3"
    text = "4"
    geographic = "5"
    scheme = "6"
    design = "7"  # Макет оформления компоновки данных
    graphic_scheme = "8"
    extension = "9"
    # todo добавить остальные типы макетов и их сериализацию


class Template2(Simple):

    def __init__(self, *, meta_obj_class=None, obj_version=None, options=None):
        super().__init__(meta_obj_class=meta_obj_class, obj_version=obj_version, options=options)
        self.data = None
        self.tmpl_type = None
        self.raw_data = None
        self.raw_header = None

    def decode_object(self, src_dir, uuid, dest_dir, dest_path, version, header):
        try:
            super(Template2, self).decode_object(src_dir, uuid, dest_dir, dest_path, version, header)
            tmpl_type = self.get_template_type(header)
            try:
                self.tmpl_type = TmplType(tmpl_type)
            except Exception as err:
                raise ExtNotImplemented(message='Неизвестный тип макета',
                                        detail=f'{tmpl_type} у {self.name} в файле {uuid}')
            self.header['type'] = self.tmpl_type.name

            # _dest_dir = os.path.join(dest_dir, dest_path)
        except Exception as err:
            raise ExtException(parent=err)
        try:
            getattr(self, f'decode_{self.tmpl_type.name}_data')(src_dir, self.new_dest_dir, True)
        except AttributeError:
            raise Exception(f'Не реализованный тип макета {self.header["type"]}')

    # @classmethod
    # def get_decode_header(cls, header_data):
    #     return header_data[0][1][2]

    @classmethod
    def get_template_type(cls, header_data):
        return header_data[0][1][1]

    @classmethod
    def get_decode_header(cls, header_data):
        return header_data[0][1][2]

    def decode_code(self, src_dir, **kwargs):
        return

    def decode_includes(self, src_dir, dest_dir, dest_path, header):
        return []

    def decode_active_doc_data(self, src_dir, dest_dir, write):
        self.decode_scheme_data(src_dir, dest_dir, write)

    def decode_geographic_data(self, src_dir, dest_dir, write):
        self.decode_scheme_data(src_dir, dest_dir, write)

    def decode_design_data(self, src_dir, dest_dir, write):
        self.decode_scheme_data(src_dir, dest_dir, write)

    def decode_graphic_scheme_data(self, src_dir, dest_dir, write):
        self.decode_scheme_data(src_dir, dest_dir, write)

    def decode_scheme_data(self, src_dir, dest_dir, write, *, extension='bin'):
        try:
            shutil.copy2(
                os.path.join(src_dir, f'{self.header["uuid"]}.0'),
                os.path.join(self.new_dest_dir, f'{self.new_dest_file_name}.{extension}')
            )
        except FileNotFoundError:
            return
            # self.decode_text_data(src_dir, dest_dir, write) ??? что это

    def decode_table_data(self, src_dir, dest_dir, write):
        self.decode_scheme_data(src_dir, dest_dir, write, extension='mxl')

    def decode_text_data(self, src_dir, dest_dir, write):
        try:
            self.data, encoding = helper.txt_read_detect_encoding(src_dir, f'{self.header["uuid"]}.0')
        except FileNotFoundError:
            return
        if write:
            helper.txt_write(self.data, self.new_dest_dir, f'{self.new_dest_file_name}.txt', encoding=encoding)

    def decode_extension_data(self, src_dir, dest_dir, write):
        self.decode_base64_data(src_dir, dest_dir, write)

    def decode_base64_data(self, src_dir, dest_dir, write):
        filename = f'{self.header["uuid"]}.0'
        try:
            data = helper.brace_file_read(src_dir, filename)
        except BigBase64:
            shutil.copy2(os.path.join(src_dir, filename),
                         os.path.join(self.new_dest_dir, f'{self.new_dest_file_name}.c1b64'))
            return
        except FileNotFoundError:
            file_name = f'{self.header["uuid"]}.0'
            if os.path.isfile(os.path.join(src_dir, file_name)):
                # todo сюда можно вставить выдергивание бинарника из файла если кому то хочется
                shutil.copy2(
                    os.path.join(src_dir, file_name),
                    os.path.join(self.new_dest_dir, f'{self.new_dest_file_name}')
                )
            return
        if data[0][1] and data[0][1][0]:
            self.data = b64decode(data[0][1][0][8:])
            data[0][1][0] = '"данные в отдельном файле"'
            if write:
                extension = helper.get_extension_from_comment(self.header['comment'])
                helper.bin_write(self.data, self.new_dest_dir, f'{self.new_dest_file_name}.{extension}')

    def decode_html_data(self, src_dir, dest_dir, write):
        self._decode_html_data(src_dir, dest_dir, self.new_dest_file_name)

    def decode_header(self, header_data, *, id_in_separate_file=True):
        self.tmpl_type = TmplType(self.get_template_type(header_data))
        self.header = {
            'type': self.tmpl_type.name,
            'header': header_data
        }
        _header = self.get_decode_header(header_data)
        helper.decode_header(self, _header)
        self.uuid = self.header['uuid']
        self.name = self.header['name']


class Template(MetaDataObject):
    versions = {
        '2': Template2
    }


# --- MetaDataObject/CommonTemplate4.py ---
class CommonTemplate4(Template2):
    @classmethod
    def get_decode_header(cls, header_data):
        return header_data[0][1][1]

    @classmethod
    def get_template_type(cls, header_data):
        return header_data[0][1][2]


# --- MetaDataObject/CommonTemplate.py ---
class CommonTemplate(MetaDataObject):
    versions = {
        '4': CommonTemplate4
    }


# --- MetaDataObject/AccountingRegister.py ---
class AccountingRegister(Container):
    ext_code = {
        'obj': 6,
        'mgr': 7
    }
    help_file_number = 5

    @classmethod
    def get_decode_header(cls, header):
        obj_version = int(header[0][1][0])
        if obj_version > 21:
            return header[0][1][16][1]
        else:
            return header[0][1][15][1]


# --- MetaDataObject/AccountingRegisterCommand.py ---
class AccountingRegisterCommand(IncludeSimple):
    @classmethod
    def get_decode_header(cls, header_data):
        return header_data[0][1][2][9]


# --- MetaDataObject/AccountingRegisterForm.py ---
class AccountingRegisterForm(Form0):
    pass


# --- MetaDataObject/AccumulationRegister.py ---
class AccumulationRegister(Container):
    ext_code = {
        'mgr': 2,  # модуль менеджера
        'obj': 1,  # Модуль набора записей
    }
    help_file_number = 0
    predefined_file_number = 3  # Агрегаты

    @classmethod
    def get_decode_header(cls, header_data):
        return header_data[0][1][13][1]


# --- MetaDataObject/AccumulationRegisterCommand.py ---
class AccumulationRegisterCommand(IncludeSimple):
    @classmethod
    def get_decode_header(cls, header_data):
        return header_data[0][1][2][9]


# --- MetaDataObject/AccumulationRegisterForm.py ---
class AccumulationRegisterForm(Form1):
    pass


# --- MetaDataObject/Bot.py ---
class Bot(SimpleNameFolder):
    ext_code = {
        'obj': 1,
    }


# --- MetaDataObject/BusinessProcess.py ---
class BusinessProcess(Container):
    ext_code = {
        'obj': 6,
        'mgr': 8
    }
    help_file_number = 5
    pass

    def decode_object(self, src_dir, file_name, dest_dir, dest_path, version, header_data):
        super().decode_object(src_dir, file_name, dest_dir, dest_path, version, header_data)
        try:
            package = helper.bin_read(src_dir, f'{self.header["uuid"]}.7')
            helper.bin_write(package, self.new_dest_dir, 'Карта маршрута.bin')
        except FileNotFoundError:
            return


# --- MetaDataObject/BusinessProcessCommand.py ---
class BusinessProcessCommand(IncludeSimple):
    pass


# --- MetaDataObject/BusinessProcessForm.py ---
class BusinessProcessForm(Form0):
    pass


# --- MetaDataObject/CalculationRegister.py ---
class CalculationRegister(Container):
    ext_code = {
        'obj': 1,
        'mgr': 2
    }
    help_file_number = 0

    @classmethod
    def get_decode_header(cls, header):
        return header[0][1][15][1]


# --- MetaDataObject/CalculationRegisterCommand.py ---
class CalculationRegisterCommand(IncludeSimple):
    @classmethod
    def get_decode_header(cls, header_data):
        return header_data[0][1][2][9]


# --- MetaDataObject/CalculationRegisterForm.py ---
class CalculationRegisterForm(Form1):
    pass


# --- MetaDataObject/CalculationRegisterRecalculations.py ---
class CalculationRegisterRecalculations(Container):
    pass

    @classmethod
    def get_decode_header(cls, header):
        return header[0][1][7][1]


# --- MetaDataObject/Catalog.py ---
class Catalog(FormContainer):
    ext_code = {
        'mgr': '3',  # модуль менеджера Справочника
        'obj': '0',  # модуль объекта Справочника
    }
    help_file_number = 1
    predefined_file_number = '1c'

    @classmethod
    def get_decode_header(cls, header):
        return header[0][1][9][1]


# --- MetaDataObject/CatalogCommand.py ---
class CatalogCommand(IncludeSimple):
    pass


# --- MetaDataObject/CatalogForm.py ---
class CatalogForm(Form1):
    pass


# --- MetaDataObject/ChartOfAccounts.py ---
class ChartOfAccounts(Container):
    ext_code = {
        'obj': 14,
        'mgr': 15
    }
    help_file_number = 5
    predefined_file_number = 9

    @classmethod
    def get_decode_header(cls, header):
        return header[0][1][15][1]

    # @classmethod
    # def get_decode_includes(cls, header_data):
    #     return super().get_decode_includes(header_data)


# --- MetaDataObject/ChartOfAccountsCommand.py ---
class ChartOfAccountsCommand(IncludeSimple):
    pass


# --- MetaDataObject/ChartOfAccountsForm.py ---
class ChartOfAccountsForm(Form0):
    pass


# --- MetaDataObject/ChartOfCalculationTypes.py ---
class ChartOfCalculationTypes(Container):
    ext_code = {
        'obj': 0,
        'mgr': 3
    }
    help_file_number = 1
    predefined_file_number = 2

    @classmethod
    def get_decode_header(cls, header):
        return header[0][1][1][1]


# --- MetaDataObject/ChartOfCalculationTypesCommand.py ---
class ChartOfCalculationTypesCommand(IncludeSimple):
    pass


# --- MetaDataObject/ChartOfCalculationTypesForm.py ---
class ChartOfCalculationTypesForm(Form1):
    pass


# --- MetaDataObject/ChartOfCharacteristicType.py ---
class ChartOfCharacteristicType(Container):
    ext_code = {
        'mgr': '16',  # модуль менеджера Плана видов характеристик
        'obj': '15',  # модуль менеджера Плана видов характеристик
    }
    help_file_number = 5
    predefined_file_number = 7

    @classmethod
    def get_decode_header(cls, header_data):
        return header_data[0][1][13][1]


# --- MetaDataObject/ChartOfCharacteristicTypeCommand.py ---
class ChartOfCharacteristicTypeCommand(IncludeSimple):
    pass


# --- MetaDataObject/ChartOfCharacteristicTypeForm.py ---
class ChartOfCharacteristicTypeForm(Form0):
    # @classmethod
    # def get_form_root(cls, header_data):
    #     obj_version = header_data[0][1][0]
    #     if obj_version == '9':
    #         return header_data
    #     else:
    #         return header_data[0]

    pass


# --- MetaDataObject/CommandGroup.py ---
class CommandGroup(Simple):
    @classmethod
    def get_decode_header(cls, header_data):
        return header_data[0][1][6]

    pass


# --- MetaDataObject/CommonAttribute.py ---
class CommonAttribute(Simple):
    # ext_code = {
    #     'mgr': '1',  # модуль менеджера Константы
    #     'obj': '0',  # модуль менеджера значения Константы
    # }

    @classmethod
    def get_decode_header(cls, header_data):
        return header_data[0][1][1][1][1]


# --- MetaDataObject/CommonCommand.py ---
class CommonCommand(Simple):
    ext_code = {'obj': 2}
    help_file_number = 1

    @classmethod
    def get_decode_header(cls, header_data):
        return header_data[0][1][1][2][9]


# --- MetaDataObject/CommonForm.py ---
class CommonForm(Form1):

    pass


# --- MetaDataObject/CommonModule.py ---
class CommonModule(SimpleNameFolder):
    ext_code = {'obj': 0}
    pass


# --- MetaDataObject/CommonPicture.py ---
class CommonPicture(Simple):
    def __init__(self, *, meta_obj_class=None, obj_version=None, options=None):
        super().__init__(meta_obj_class=meta_obj_class, obj_version=obj_version, options=options)
        self.ext_code = {}
        self.data = None
        self.raw_data = None

    def decode_object(self, src_dir, file_name, dest_dir, dest_path, version, header_data):
        try:
            super().decode_object(src_dir, file_name, dest_dir, dest_path, version, header_data)
            try:
                self.header['info'] = helper.brace_file_read(src_dir, f'{self.header["uuid"]}.0')
            except FileNotFoundError:
                return
            if self.header['info'][0][2] and self.header['info'][0][2][0] and self.header['info'][0][2][0][0]:
                bin_data = self._extract_b64_data(self.header['info'][0][2][0])

                extension = helper.get_extension_from_comment(self.header['comment'])
                if dest_dir:
                    helper.bin_write(bin_data, self.new_dest_dir, f'{self.new_dest_file_name}.{extension}')
        except Exception as err:
            raise ExtException(parent=err)


# --- MetaDataObject/Constant.py ---
class Constant(Simple):
    ext_code = {
        'mgr': '1',  # модуль менеджера Константы
        'obj': '0',  # модуль менеджера значения Константы
    }

    @classmethod
    def get_decode_header(cls, header_data):
        return header_data[0][1][1][1][1]


# --- MetaDataObject/DataProcessor.py ---
class DataProcessor(FormContainer):
    ext_code = {
        'mgr': '2',  # модуль менеджера
        'obj': '0',  # модуль объекта
    }
    help_file_number = 1

    @classmethod
    def get_decode_header(cls, header_data):
        return header_data[0][1][3][1]

    def decode_ids(self):
        data_id = super().decode_ids()

        if self.obj_version.startswith('803'):
            manager_data = self.header['header'][0][1]
            data_id['manager_uuid1'] = manager_data[1]
            data_id['manager_uuid2'] = manager_data[2]
            data_id['manager_uuid3'] = manager_data[7]
            data_id['manager_uuid4'] = manager_data[8]
            manager_data[1] = 'manager_uuid1 в файле id'
            manager_data[2] = 'manager_uuid2 в файле id'
            manager_data[7] = 'manager_uuid3 в файле id'
            manager_data[8] = 'manager_uuid4 в файле id'

        return data_id


# --- MetaDataObject/DataProcessorCommand.py ---
class DataProcessorCommand(IncludeSimple):
    @classmethod
    def get_decode_header(cls, header_data):
        return header_data[0][1][2][9]
    pass


# --- MetaDataObject/DefinedType.py ---
class DefinedType(Simple):
    @classmethod
    def get_decode_header(cls, header_data):
        return header_data[0][1][3]


# --- MetaDataObject/Document.py ---
class Document(FormContainer):
    help_file_number = 1
    ext_code = {
        'obj': 0,
        'mgr': 2
    }

    @classmethod
    def get_decode_header(cls, header_data):
        return header_data[0][1][9][1]


# --- MetaDataObject/DocumentCommand.py ---
class DocumentCommand(IncludeSimple):
    pass


# --- MetaDataObject/DocumentForm.py ---
class DocumentForm(Form1):
    pass


# --- MetaDataObject/DocumentJournal.py ---
class DocumentJournal(Container):
    help_file_number = 0
    ext_code = {
        'mgr': 1
    }

    @classmethod
    def get_decode_header(cls, header):
        return header[0][1][3][1]


# --- MetaDataObject/DocumentJournalCommand.py ---
class DocumentJournalCommand(IncludeSimple):
    @classmethod
    def get_decode_header(cls, header_data):
        return header_data[0][1][2][9]


# --- MetaDataObject/DocumentJournalForm.py ---
class DocumentJournalForm(Form1):
    pass


# --- MetaDataObject/DocumentNumerators.py ---
class DocumentNumerators(Simple):
    pass
    # @classmethod
    # def get_decode_header(cls, header_data):
    #     return header_data[0][1][1]


# --- MetaDataObject/Enum.py ---
class Enum(Container):
    @classmethod
    def get_decode_header(cls, header_data):
        return header_data[0][1][5][1]


# --- MetaDataObject/EnumCommand.py ---
class EnumCommand(IncludeSimple):
    pass


# --- MetaDataObject/EnumForm.py ---
class EnumForm(Form1):
    pass


# --- MetaDataObject/EventSubscription.py ---
class EventSubscription(SimpleWithInfo):
    pass


# --- MetaDataObject/ExchangePlan.py ---
class ExchangePlan(Container):
    ext_code = {'obj': 2, 'mgr': 3}
    help_file_number = 0
    pass

    @classmethod
    def get_decode_header(cls, header):
        return header[0][1][12]

    def decode_object(self, src_dir, file_name, dest_dir, dest_path, version, header_data):
        super().decode_object(src_dir, file_name, dest_dir, dest_path, version, header_data)
        try:
            self.header['info'] = helper.brace_file_read(src_dir, f'{self.header["uuid"]}.1')
        except FileNotFoundError:
            self.header['info'] = None


# --- MetaDataObject/ExchangePlanCommand.py ---
class ExchangePlanCommand(IncludeSimple):
    pass


# --- MetaDataObject/ExchangePlanForm.py ---
class ExchangePlanForm(Form1):
    pass


# --- MetaDataObject/ExternalDataSource.py ---
class ExternalDataSource(Container):
    @classmethod
    def get_decode_header(cls, header_data):
        return header_data[0][1][1][1]


# --- MetaDataObject/ExternalDataSourceCube.py ---
class ExternalDataSourceCube(Container):
    ext_code = {'obj': 2, 'mgr': 1}

    @classmethod
    def get_decode_header(cls, header_data):
        return header_data[0][1][1][1]


# --- MetaDataObject/ExternalDataSourceCubeCommand.py ---
class ExternalDataSourceCubeCommand(IncludeSimple):
    pass


# --- MetaDataObject/ExternalDataSourceCubeForm.py ---
class ExternalDataSourceCubeForm(Form0):
    pass


# --- MetaDataObject/ExternalDataSourceTable.py ---
class ExternalDataSourceTable(Container):
    @classmethod
    def get_decode_header(cls, header_data):
        return header_data[0][1][1][1]


# --- MetaDataObject/ExternalDataSourceTableCommand.py ---
class ExternalDataSourceTableCommand(IncludeSimple):
    pass


# --- MetaDataObject/ExternalDataSourceTableForm.py ---
class ExternalDataSourceTableForm(Form0):
    pass


# --- MetaDataObject/FilterCriterion.py ---
class FilterCriterion(Container):
    @classmethod
    def get_decode_header(cls, header_data):
        return header_data[0][1][5][1]


# --- MetaDataObject/FilterCriterionCommand.py ---
class FilterCriterionCommand(IncludeSimple):
    pass


# --- MetaDataObject/FilterCriterionForm.py ---
class FilterCriterionForm(Form1):
    pass


# --- MetaDataObject/FunctionalOption.py ---
class FunctionalOption(Simple):
    @classmethod
    def get_decode_header(cls, header_data):
        return header_data[0][1][1]


# --- MetaDataObject/FunctionalOptionsParameter.py ---
class FunctionalOptionsParameter(Simple):
    # ext_code = {
    #     'mgr': '1',  # модуль менеджера Константы
    #     'obj': '0',  # модуль менеджера значения Константы
    # }

    @classmethod
    def get_decode_header(cls, header_data):
        return header_data[0][1][1]


# --- MetaDataObject/HTTPService.py ---
class HTTPService(Container):
    pass

    @classmethod
    def get_decode_header(cls, header):
        return header[0][1][2]


# --- MetaDataObject/InformationRegister.py ---
class InformationRegister(Container):
    ext_code = {
        'mgr': 2,  # модуль менеджера
        'obj': 1,  # Модуль набора записей
    }
    help_file_number = 0

    @classmethod
    def get_decode_header(cls, header_data):
        return header_data[0][1][15][1]


# --- MetaDataObject/InformationRegisterCommand.py ---
class InformationRegisterCommand(IncludeSimple):
    @classmethod
    def get_decode_header(cls, header_data):
        return header_data[0][1][2][9]


# --- MetaDataObject/InformationRegisterForm.py ---
class InformationRegisterForm(Form1):
    pass


# --- MetaDataObject/IntegrationService.py ---
class IntegrationService(Container):
    @classmethod
    def get_decode_header(cls, header_data):
        return header_data[0][1][1]


# --- MetaDataObject/Interface.py ---
class Interface(SimpleWithInfo):
    @classmethod
    def get_decode_header(cls, header_data):
        return header_data[0][1][2]


# --- MetaDataObject/Language.py ---
class Language(Simple):
    pass


# --- MetaDataObject/Report.py ---
class Report(Container):
    ext_code = {
        'mgr': '2',  # модуль менеджера Отчета
        'obj': '0',  # модуль Отчета
    }
    help_file_number = 1

    @classmethod
    def get_decode_header(cls, header_data):
        return header_data[0][1][3][1]


# --- MetaDataObject/ReportCommand.py ---
class ReportCommand(IncludeSimple):
    @classmethod
    def get_decode_header(cls, header_data):
        return header_data[0][1][2][9]


# --- MetaDataObject/ReportForm.py ---
class ReportForm(Form):
    pass


# --- MetaDataObject/Role.py ---
class Role(SimpleWithInfo):
    pass


# --- MetaDataObject/ScheduledJob.py ---
class ScheduledJob(SimpleWithInfo):
    pass


# --- MetaDataObject/Sequences.py ---
class Sequences(Container):
    pass

    @classmethod
    def get_decode_header(cls, header):
        return header[0][1][7][1]


# --- MetaDataObject/SessionParameter.py ---
class SessionParameter(Simple):
    @classmethod
    def get_decode_header(cls, header_data):
        return header_data[0][1][1][1]


# --- MetaDataObject/SettingsStorage.py ---
class SettingsStorage(Container):
    ext_code = {
        'mgr': 8,
    }
    # help_file_number = 5

    @classmethod
    def get_decode_header(cls, header):
        return header[0][1][1][1]


# --- MetaDataObject/SettingsStorageForm.py ---
class SettingsStorageForm(Form1):
    pass


# --- MetaDataObject/Style.py ---
class Style(SimpleWithInfo):
    pass


# --- MetaDataObject/StyleItem.py ---
class StyleItem(Simple):
    @classmethod
    def get_decode_header(cls, header_data):
        return header_data[0][1][3]

    pass


# --- MetaDataObject/Subsystem.py ---
class Subsystem(Container):
    help_file_number = 0
    ext_code = {}

    # todo Subsystem не учтено в старых конфакх может быть папка 0 c image и info

    def decode_object(self, src_dir, file_name, dest_dir, dest_path, version, header_data):
        super(Subsystem, self).decode_object(src_dir, file_name, dest_dir, dest_path, version, header_data)
        try:
            self.header['info'] = helper.brace_file_read(src_dir, f'{self.header["uuid"]}.1')
        except FileNotFoundError:
            self.header['info'] = None
            return
        pass


# --- MetaDataObject/Task.py ---
class Task(Container):
    ext_code = {
        'obj': 6,
        'mgr': 7
    }
    help_file_number = 5
    # @classmethod
    # def get_decode_header(cls, header):
    #     return header[0][1][9][1]


# --- MetaDataObject/TaskCommand.py ---
class TaskCommand(IncludeSimple):
    pass


# --- MetaDataObject/TaskForm.py ---
class TaskForm(Form0):
    pass


# --- MetaDataObject/WSReference.py ---
class WSReference(Simple):
    ext_code = {}

    @classmethod
    def get_decode_header(cls, header):
        return header[0][1][2]

    def decode_object(self, src_dir, file_name, dest_dir, dest_path, version, header_data):
        super().decode_object(src_dir, file_name, dest_dir, dest_path, version, header_data)

        src = os.path.join(src_dir, f'{self.header["uuid"]}.0')
        dest = os.path.join(self.new_dest_dir, self.header["name"], self.__class__.__name__)
        if not os.path.isdir(src):
            return
        shutil.copytree(src, dest)


# --- MetaDataObject/WebService.py ---
class WebService(Container):
    pass

    @classmethod
    def get_decode_header(cls, header):
        return header[0][1][2]


# --- MetaDataObject/XDTOPackage.py ---
class XDTOPackage(MetaDataObject):

    def decode_object(self, src_dir, file_name, dest_dir, dest_path, version, header_data):
        super().decode_object(src_dir, file_name, dest_dir, dest_path, version, header_data)
        try:
            package = helper.bin_read(src_dir, f'{self.header["uuid"]}.0')
            helper.bin_write(package, self.new_dest_dir, f'{self.new_dest_file_name}.bin')
        except FileNotFoundError:
            return

    def decode_includes(self, src_dir, dest_dir, dest_path, header):
        return []
