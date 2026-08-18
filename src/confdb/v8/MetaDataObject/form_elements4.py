"""Элементы форм 4.x (слитый порт v8unpack FormElements4, MIT)."""
from enum import Enum
from .. import helper
from ..ext_exception import ExtException
from ..helper import calc_offset
from ..helper import FuckingBrackets


# --- MetaDataObject/Form/FormElements4/FormElement.py ---
def check_count_element(counters, raw_data):
    # counters - позиции указывающие на счетчики, если не 0 то за ним идет столько записей размера size
    index = 0
    var_len = 0
    for counter_index, size in counters:
        index += counter_index
        if size:
            value = int(raw_data[index])
            index += value * size
            var_len += value * size
    return len(raw_data) - var_len


class FormItemTypes(Enum):
    Field = '77ffcc29-7f2d-4223-b22f-19666e7250ba'
    Button = 'a9f3b1ac-f51b-431e-b102-55a69acdecad'
    Decoration = '3d3cb80c-508b-41fa-8a18-680cdf5f1712'
    Group = 'cd5394d0-7dda-4b56-8927-93ccbe967a01'
    Table = '143c00f7-a42d-4cd7-9189-88e4467dc768'
    ItemAddition = 'c5259a1d-518a-4afd-b98d-0176027e4feb'


class FormElement:

    @classmethod
    def get_name_node_offset(cls, raw_data):
        return calc_offset([(3, 1), (1, 0)], raw_data)

    @classmethod
    def get_prop_link_offset(cls, raw_data):
        return None

    @classmethod
    def get_command_link_offset(cls, raw_data):
        return None

    @classmethod
    def decode(cls, form, path, raw_data):
        try:
            name_offset = cls.get_name_node_offset(raw_data)
            name = raw_data[name_offset]
            prop_link_offset = cls.get_prop_link_offset(raw_data)
            prop = ''
            if prop_link_offset is not None:
                prop_link = raw_data[prop_link_offset]
                if prop_link and int(prop_link[0]):
                    prop = []
                    prop_src = form.props_index
                    for i in range(int(prop_link[0])):
                        prop_id = prop_link[i + 1][0]
                        try:
                            prop_name = prop_src[prop_id]['name']
                            prop_src = prop_src[prop_id]['child']
                            prop.append(prop_name)
                        except KeyError:
                            prop = None
                            break
                    if prop:
                        prop = '.'.join(prop)

            command_link_offset = cls.get_command_link_offset(raw_data)
            command = []
            if command_link_offset is not None:
                command_link = raw_data[command_link_offset]
                if command_link and int(command_link[0]):
                    command = form.commands_index.get(command_link[0])

            if not isinstance(name, str) or not name:
                raise ExtException(message='form elem name not string')
            data = dict(raw=raw_data, ver=4)
            if prop:
                data['ПутьКДанным'] = prop
            if command:
                data['ИмяКоманды'] = command
            name = helper.str_decode(name)
            key = f'{path}/{name}' if path else name
            form.elements_data[key] = data
            return dict(
                name=name,
                type=cls.__name__,
            )
        except Exception as err:
            raise ExtException(parent=err)

    @classmethod
    def decode_list(cls, form, raw_data, index_element_count, path=''):
        try:
            result = []
            element_count = int(raw_data[index_element_count])
            if not element_count:
                return

            for i in range(element_count):
                metadata_type_uuid = raw_data[index_element_count + i * 2 + 1]
                elem_raw_data = raw_data[index_element_count + i * 2 + 2]
                try:
                    metadata_type = FormItemTypes(metadata_type_uuid)
                except ValueError:
                    raise ExtException(
                        message='Неизвестный тип элемента формы',
                        detail=f'{form.__class__.__name__} {form.header.get("name")} : {metadata_type_uuid}'
                    )
                elem_data = cls.decode_elem(metadata_type.name, form, path, elem_raw_data)
                result.append(elem_data)

            raw_data[index_element_count] = 'Дочерние элементы отдельно'
            del raw_data[index_element_count + 1:index_element_count + 1 + element_count * 2]
            return result
        except helper.FuckingBrackets as err:
            raise err
        except Exception as err:
            raise ExtException(parent=err)

    @classmethod
    def decode_elem(cls, metadata_type_name, form, path, elem_raw_data):
        try:
            handler = cls.get_class_form_elem(metadata_type_name)
        except Exception as err:
            raise ExtException(
                parent=err,
                message='Проблема с парсером элемента формы',
                detail=f'{metadata_type_name} - {err}'
            )
        try:
            return handler.decode(form, path, elem_raw_data)
        except helper.FuckingBrackets as err:
            raise err from err
        except Exception as err:
            raise ExtException(
                parent=err,
                detail=f'{metadata_type_name} - {err}',
                message='Ошибка разбора элемента формы'
            )

    @staticmethod
    def get_class_form_elem(name):
        cls = globals().get(name)
        if not isinstance(cls, type):
            raise AttributeError(f'Нет класса элемента формы 4.x: {name}')
        return cls


class _FormRoot:
    element_node_offset = [[2, 1], [3, 1], [4, 1]]
    name = ''
    index = 0
    index_name = 0

    @classmethod
    def decode(cls, form, raw_data):
        return dict(
            name=helper.str_decode(raw_data[cls.index_name]),
            raw=raw_data
        )

    @classmethod
    def decode_list(cls, form, raw_data):
        try:
            if len(raw_data) <= cls.index:
                return
            items = raw_data[cls.index]
            index_element_count = 1
            element_count = int(items[index_element_count])
            if not element_count:
                return

            result = []
            for i in range(element_count):
                result.append(cls.decode(form, items[i + index_element_count + 1]))
            items[index_element_count] = 'Дочерние элементы отдельно'
            del items[index_element_count + 1:index_element_count + 1 + element_count]
            return result
        except Exception as err:
            pass


class FormParams(_FormRoot):
    name = 'params'
    index = 4
    index_name = 1


class FormProps(_FormRoot):
    name = 'props'
    index = 3
    index_name = 3
    child_offset = 13

    @classmethod
    def decode(cls, form, raw_data):
        try:
            result = dict(
                name=helper.str_decode(raw_data[cls.index_name]),
                id=raw_data[1][0],
                raw=raw_data
            )
            child_count = int(raw_data[cls.child_offset])
            pattern = raw_data[5]
            if pattern and len(pattern) > 1 and pattern[1][0] == '"#"' and form.form.parent_container_uuid == \
                    pattern[1][1]:
                pattern[1][1] = "Родитель"

            if child_count:
                result['child'] = []
                for i in range(child_count):
                    child = raw_data[cls.child_offset + i + 1]
                    result['child'].append(cls.decode_child(child))
                raw_data[cls.child_offset] = "отдельно"
                del raw_data[cls.child_offset + 1:cls.child_offset + 1 + child_count]

            return result
        except Exception as err:
            raise ExtException(parent=err)

    @classmethod
    def decode_child(cls, raw_data):
        return dict(
            name=helper.str_decode(raw_data[cls.index_name]),
            id=raw_data[1],
            raw=raw_data
        )


class FormCommands(_FormRoot):
    name = 'commands'
    index = 5
    index_name = 2

    @classmethod
    def decode(cls, form, raw_data):
        result = dict(
            name=helper.str_decode(raw_data[cls.index_name]),
            id=raw_data[1][0],
            raw=raw_data
        )
        return result


# --- MetaDataObject/Form/FormElements4/Button.py ---
class Button(FormElement):
    @classmethod
    def get_name_node_offset(cls, raw_data):
        return calc_offset([(3, 1), (2, 0)], raw_data)

    @classmethod
    def get_command_link_offset(cls, raw_data):
        return calc_offset([(3, 1), (5, 0)], raw_data)


# --- MetaDataObject/Form/FormElements4/Decoration.py ---
class Decoration(FormElement):
    @classmethod
    def get_name_node_offset(cls, raw_data):
        return calc_offset([(3, 1), (1, 1), (2, 0)], raw_data)

    # @classmethod
    # def decode(cls, form, path, raw_data):
    #     # _version = raw_data[0]
    #     result = super().decode(form, path, raw_data)
    #     return result

    # @classmethod
    # def decode_5(cls, form, raw_data):
    #
    #     try:
    #         size = check_count_element([
    #             (3, 1), (1, 1), (15, 1)
    #         ], raw_data)
    #     except TypeError:
    #         raise FuckingBrackets(detail=cls.__name__)
    #     except Exception as err:
    #         raise ExtException(parent=err)
    #     if size != 34:
    #         raise FuckingBrackets(detail=cls.__name__)
    #     result = super().decode(form, raw_data)
    #     return result
    #
    # @classmethod
    # def decode_11(cls, form, raw_data):
    #     try:
    #         size = check_count_element([
    #             (3, 1), (1, 1), (15, 1), (5, 1)
    #         ], raw_data)
    #     except TypeError:
    #         raise FuckingBrackets(detail=cls.__name__)
    #     except Exception as err:
    #         raise ExtException(parent=err)
    #     if size != 33:
    #         raise FuckingBrackets(detail=cls.__name__)
    #     result = super().decode(form, raw_data)
    #     return result
    #
    # @classmethod
    # def decode_12(cls, form, raw_data):
    #     try:
    #         size = check_count_element([
    #             (3, 1), (1, 1), (15, 1), (5, 1)
    #         ], raw_data)
    #     except TypeError:
    #         raise FuckingBrackets(detail=cls.__name__)
    #     except Exception as err:
    #         raise ExtException(parent=err)
    #     if size != 34:
    #         raise FuckingBrackets(detail=cls.__name__)
    #     result = super().decode(form, raw_data)
    #     return result
    #
    # @classmethod
    # def _decode(cls, form, raw_data):
    #     try:
    #         size = check_count_element([
    #             (3, 1), (1, 1), (15, 1), (5, 1)
    #         ], raw_data)
    #     except TypeError:
    #         raise FuckingBrackets(detail=cls.__name__)
    #     except Exception as err:
    #         raise ExtException(parent=err)
    #     if size != 34:
    #         raise FuckingBrackets(detail=cls.__name__)
    #     result = super().decode(form, raw_data)
    #     return result


# --- MetaDataObject/Form/FormElements4/Field.py ---
class Field(FormElement):
    @classmethod
    def get_name_node_offset(cls, raw_data):
        return calc_offset([(3, 1), (1, 1), (2, 0)], raw_data)

    @classmethod
    def get_prop_link_offset(cls, raw_data):
        return calc_offset([(3, 1), (1, 1), (7, 0)], raw_data)


# --- MetaDataObject/Form/FormElements4/Group.py ---
class Group(FormElement):
    pass

    @classmethod
    def get_name_node_offset(cls, raw_data):
        return calc_offset([(3, 1), (1, 1), (2, 0)], raw_data)

    @classmethod
    def decode(cls, form, path, raw_data):
        try:
            size = check_count_element([
                (3, 1), (1, 1), (17, 2)
            ], raw_data)
        except Exception as err:
            raise ExtException(parent=err)
        if raw_data[0] == '22' and size < 20:
            raise FuckingBrackets(detail=cls.__name__)

        data = super().decode(form, path, raw_data)
        index = calc_offset([(3, 1), (1, 1), (17, 0)], raw_data)
        new_path = f"{path}/{data['name']}" if path else data['name']
        new_path = new_path.replace('includr_', 'include_')
        data['child'] = cls.decode_list(form, raw_data, index, new_path)
        return data


# --- MetaDataObject/Form/FormElements4/ItemAddition.py ---
class ItemAddition(FormElement):
    @classmethod
    def get_name_node_offset(cls, raw_data):
        return calc_offset([(3, 1), (3, 0)], raw_data)


# --- MetaDataObject/Form/FormElements4/Table.py ---
class Table(FormElement):

    @classmethod
    def get_name_node_offset(cls, raw_data):
        return calc_offset([(4, 1), (1, 0)], raw_data)

    @classmethod
    def get_prop_link_offset(cls, raw_data):
        return calc_offset([(4, 1), (7, 0)], raw_data)

    @classmethod
    def decode(cls, form, path, raw_data):
        try:
            size = check_count_element([
                (4, 1), (50, 2), (7, 2)
            ], raw_data)
        except Exception as err:
            raise ExtException(parent=err)
        if raw_data[0] == '55' and size != 99:
            raise FuckingBrackets()
        data = super().decode(form, path, raw_data)
        cls.decode_columns(form, path, raw_data, data)
        return data

    @classmethod
    def decode_columns(cls, form, path, raw_data, data):
        try:
            index = calc_offset([
                (4, 1), (50, 2), (7, 0)
            ], raw_data)
            data['child'] = cls.decode_list(form, raw_data, index, f"{path}/{data['name']}" if path else data['name'])
        except Exception as err:
            raise ExtException(parent=err)


# --- MetaDataObject/Form/FormElements4/FormElements4.py ---
class FormElements4:

    def __init__(self, form):
        self.form = form
        self.props_index = None
        self.commands_index = None

    @property
    def elements_data(self):
        return self.form.elements_data

    @property
    def elements_tree(self):
        return self.form.elements_tree

    def decode(self, src_dir, dest_dir, dest_path, raw_data):
        try:
            self.form.props = FormProps.decode_list(self, raw_data)
            self.form.commands = FormCommands.decode_list(self, raw_data)
            self.decode_elements(raw_data)
            self.form.params = FormParams.decode_list(self, raw_data)
        except Exception as err:
            raise ExtException(parent=err)

    def create_prop_index_by_id(self):
        self.props_index = {}
        if self.form.props:
            for prop in self.form.props:
                self.props_index[prop['id']] = {'name': prop['name'], 'child': {}}
                childs = prop.get('child', [])
                for child in childs:
                    self.props_index[prop['id']]['child'][child['id']] = {'name': child['name'], 'child': {}}

    def create_commands_index_by_id(self):
        self.commands_index = {}
        if self.form.commands:
            for command in self.form.commands:
                self.commands_index[command['id']] = command['name']

    def create_prop_index_by_name(self):
        self.props_index = {}
        if self.form.props:
            for prop in self.form.props:
                self.props_index[prop['name']] = prop['id']
                childs = prop.get('child', [])
                for child in childs:
                    self.props_index[f"{prop['name']}.{child['name']}"] = prop['id'], child['id']

    def create_commands_index_by_name(self):
        self.commands_index = {}
        if self.form.commands:
            for command in self.form.commands:
                self.commands_index[command['name']] = command['raw'][1]

    def decode_elements(self, raw_data):
        try:
            index = self.get_form_elem_index(raw_data)
            root_data = raw_data[1]
            self.create_prop_index_by_id()
            self.create_commands_index_by_id()
            # index_panel_count = index[1]
            # form_panels_count = int(root_data[index_panel_count])
            # if form_panels_count:
            #     self.command_panels = [root_data[index_panel_count + 1]]
            #     root_data[index_panel_count] = 'В отдельном файле'
            #     del root_data[index_panel_count + 1]

            index_root_element_count = index[0]
            form_items_count = int(root_data[index_root_element_count])
            if form_items_count:
                self.form.elements_tree = FormElement.decode_list(self, root_data, index_root_element_count)
                self.form.elements_data = dict(sorted(self.form.elements_data.items()))
            pass
        except Exception as err:
            raise ExtException(parent=err)

    def get_form_elem_index(self, raw_data):
        try:
            root_data = raw_data[1]
            index_command_panel_count = calc_offset([(18, 2), (3, 0)], root_data)
            command_panel_count = int(root_data[index_command_panel_count])
            index_root_elem_count = index_command_panel_count + command_panel_count + 1
            return index_root_elem_count, index_command_panel_count
        except Exception as err:
            raise ExtException(
                message='случай требующий анализа, предоставьте образец формы разработчикам',
                detail=f'{self.form.name}, {err}')
