"""Элементы форм 2.7 (слитый порт v8unpack FormElements27, MIT)."""
from enum import Enum
from .. import helper
from ..ext_exception import ExtException
from ..helper import calc_offset


# --- MetaDataObject/Form/FormElements27/FormElement.py ---
class FormItemTypes(Enum):
    Field = '381ed624-9217-4e63-85db-c4c3cb87daae'
    CheckBox = '35af3d93-d7c7-4a2e-a8eb-bac87a1a3f26'
    RadioBtn = '782e569a-79a7-4a4f-a936-b48d013936ec'
    SelectField = '64483e7f-3833-48e2-8c75-2c31aac49f6e'
    CommandPanel = 'e69bf21d-97b2-4f37-86db-675aea9ec2cb'
    Button = '6ff79819-710e-4145-97cd-1618da79e3e2'
    Image = '151ef23e-6bb2-4681-83d0-35bc2217230c'
    Group = '90db814a-c75f-4b54-bc96-df62e554d67d'
    Table = 'ea83fe3a-ac3c-4cce-8045-3dddf35b28b1'
    TableField = '236a17b3-7f44-46d9-a907-75f9cdc61ab5'
    Panel = '09ccdc77-ea1a-4a6d-ab1c-3435eada2433'
    Label = '0fc7e20d-f241-460c-bdf4-5ad88e5474a5'
    ListField = '19f8b798-314e-4b4e-8121-905b2a7a03f5'
    Separator = '36e52348-5d60-4770-8e89-a16ed50a2006'
    FieldHtml = 'd92a805c-98ae-4750-9158-d9ce7cec2f20'
    Indicator = 'b1db1f86-abbb-4cf0-8852-fe6ae21650c2'
    CalendarBox = 'e3c063d8-ef92-41be-9c89-b70290b5368b'
    TrackBar = '6c06cd5d-8481-4b6f-a90a-7a97a8bb8bef'
    TextDocumentField = '14c4a229-bfc3-42fe-9ce1-2da049fd0109'
    GraphicalSchemaField = '42248403-7748-49da-b782-e4438fd7bff3'
    GeographicalSchemaField = 'ad37194e-555e-4305-b718-5dca84baf145'
    Chart = 'a8b97779-1a4b-4059-b09c-807f86d2a461'
    GanttChart = 'e5fdc112-5c84-4a16-9728-72b85692b6e2'
    PivotChart = 'a26da99e-184a-4823-b0d6-62816d38dc4e'
    Dendrogram = '984981b1-622d-4ebc-94f7-885f0cdfb59a'


class Anchor(Enum):
    top = '0'
    bottom_center = '1'
    left = '2'
    right = '3'


class FormElement:
    ver = 27
    name = 'elements'

    def __init__(self):
        self.anchored = {}

    @classmethod
    def get_class_form_elem(cls, name):
        if name == 'Panel':
            from . import form_elements26, form_elements27
            mod = form_elements26 if cls.ver == 26 else form_elements27
            return getattr(mod, name)
        return FormElement

    @classmethod
    def decode(cls, form, path, elem_raw_data):
        metadata_type_uuid = elem_raw_data[0]
        name = helper.str_decode(elem_raw_data[-2][1])
        try:
            metadata_type = FormItemTypes(metadata_type_uuid)
        except ValueError:
            raise ExtException(
                message='Неизвестный тип элемента формы',
                detail=f'{metadata_type_uuid} {name} - {form.__class__.__name__} {form.header["name"]}',
                action='Form802Element.decode'
            )
        page = elem_raw_data[-3][-5]
        elem_id = elem_raw_data[1]
        elem_data = dict(
            name=name,
            type=metadata_type.name,
            id=elem_id,
            ver=cls.ver
        )
        # cls.decode_anchored(elem_id, form.anchored, path, elem_raw_data[3], 12)
        return form.add_elem(page, path, name, elem_data, elem_raw_data)



    # @classmethod
    # def decode_anchored(cls, anchor_id, anchored, path, elem_raw_data, offset):
    #     def add_anchor():
    #         if len(anchor_data) != 3 or anchor_data[0] != '0':
    #             raise ExtException(message='Неизвестный формат данных привязки', detail=path,
    #                                dump=dict(anchor_data=anchor_data, anchor_id=anchor_id))
    #
    #         elem_id = anchor_data[1]
    #         border = Anchor(anchor_data[2]).name
    #         if elem_id not in anchored:
    #             anchored[elem_id] = []
    #         anchored[elem_id].append(dict(
    #             border=border,
    #             anchor=anchor_id,
    #             anchor_border=Anchor(str(anchor_border)).name,
    #         ))
    #     try:
    #         for anchor_border in range(4):
    #             count_anchor = int(elem_raw_data[offset])
    #             for j in range(count_anchor):
    #                 anchor_data = elem_raw_data[offset + j + 1]
    #                 add_anchor()
    #             elem_raw_data[offset] = '0'
    #             del elem_raw_data[offset + 1: offset + 1 + count_anchor]
    #             offset += 1
    #     except Exception as err:
    #         raise ExtException(parent=err)


class FormProps:
    name = 'props'
    name_index = 4


    @classmethod
    def decode(cls, form, elem_raw_data):
        try:
            elem_data = dict(
                name=helper.str_decode(elem_raw_data[cls.name_index]),
                id=elem_raw_data[0][0],
                raw=elem_raw_data
            )
            return elem_data
        except Exception as err:
            raise ExtException(parent=err)

    @classmethod
    def decode_list(cls, form, raw_data, index_element_count=0):
        result = []
        element_count = int(raw_data[index_element_count])
        if not element_count:
            return

        for i in range(element_count):
            elem_raw_data = raw_data[index_element_count + i + 1]
            result.append(cls.decode(form, elem_raw_data))

        raw_data[index_element_count] = 'Дочерние элементы отдельно'
        del raw_data[index_element_count + 1:index_element_count + 1 + element_count]
        return result


# --- MetaDataObject/Form/FormElements27/Panel.py ---
class Panel(FormElement):
    ver = 27

    def __init__(self):
        super().__init__()
        self.elements_tree = []
        self.pages = []
        self.form = None
        self._elements_tree_index = {}
        self.elements_index = {}
        self.anchor_index = [[], [], [], []]
        self.auto_include = False
        self.elements_data = {}
        self.props_index = {}
        self.last_elem_id = 1
        self.field_data_source = []

    @staticmethod
    def calc_id(path, page, name=None):
        id = []
        if path:
            id.append(path)
        if page:
            id.append(page)
        if name:
            id.append(name)
        return '/'.join(id)

    def add_elem(self, page, path, name, elem_data, elem_raw_data):
        def get_page_name():
            if page and self.pages:
                if page != str(int(page)):
                    raise ExtException(message='Не удалось определить номер страницы элемента формы',
                                       detail=f'{path} {name}')
                return self.pages[int(page)]
            return None

        try:
            page_name = get_page_name()
            page_id = self.calc_id(path, page_name, None)
            elem_tree = dict(name=elem_data['name'], type=elem_data['type'])
            if self.auto_include:
                elem_id = self.calc_id(path, page_name, name)
                parent = self.elements_tree[self._elements_tree_index[page_id]]
                parent['child'].append(elem_tree)
            else:
                elem_id = self.calc_id(path, page_name, name)
                elem_tree['page'] = page_name
                self.elements_tree.append(elem_tree)

            self.elements_index[elem_data['id']] = elem_id

            self.elements_data[elem_id] = {
                'id': elem_data['id'],
                'ver': self.ver,
                'page': page_id,
                'raw': elem_raw_data,
            }

            # anchored = self.anchored.get(elem_data['id'])
            # if anchored:
            #     self.elements_data[elem_id]['anchored'] = anchored

            if self.form:
                prop = self.form.props_index.get(elem_data['id'])
                if prop:
                    self.elements_data[elem_id]['prop'] = prop['name']

            return elem_tree, None, elem_id
        except Exception as err:
            raise ExtException(parent=err)

    @classmethod
    def decode(cls, form, path, elem_raw_data):
        try:
            self = cls()
            self.auto_include = form.auto_include
            is_child = 0 if isinstance(elem_raw_data[1], list) else 1
            elem_id = None
            if is_child:
                self.form = form
                self.form.props_index = form.form.props_index
                elem, elem_data, elem_id = super().decode(form, path, elem_raw_data)
                new_path = elem_id.replace('includr_', 'include_')
                self.decode_pages(new_path, elem_raw_data[1 + is_child][1])
                self.decode_elements(new_path, elem_raw_data[-1])
                elem['child'] = self.elements_tree
                form.elements_data.update(self.elements_data)
            else:
                self.form = form
                self.decode_pages(path, elem_raw_data[1 + is_child][1])
                self.decode_elements('', elem_raw_data[2])
            return self.elements_tree, self.elements_data, elem_id
        except Exception as err:
            raise ExtException(parent=err)

    def decode_elements(self, path, raw_data):
        try:
            # result = []
            element_count = int(raw_data[0])
            if not element_count:
                return
            panel_elements = []
            for i in range(element_count):
                elem_raw_data = raw_data[i + 1]
                metadata_type_uuid = elem_raw_data[0]
                try:
                    metadata_type_name = FormItemTypes(metadata_type_uuid)
                except ValueError:
                    raise ExtException(
                        message='Неизвестный тип элемента формы',
                        detail=f'{self.__class__.__name__}: {metadata_type_uuid}'
                    )
                try:
                    handler = self.get_class_form_elem(metadata_type_name.name)
                except Exception as err:
                    raise ExtException(
                        parent=err,
                        message='Проблема с парсером элемента формы',
                        detail=f'{metadata_type_name} - {err}'
                    )

                try:
                    elem_tree, elem_data, elem_id = handler.decode(self, path, elem_raw_data)
                    panel_elements.append(elem_id)
                except helper.FuckingBrackets as err:
                    raise err from err
                except Exception as err:
                    raise ExtException(
                        parent=err,
                        detail=f'{metadata_type_name} - {err}',
                        message='Ошибка разбора элемента формы'
                    )

                # result.append(res)
            raw_data[0] = 'Дочерние элементы отдельно'
            del raw_data[1:1 + element_count]

            if self.auto_include:
                # меняем ид элеменетов на названия полей
                for elem in panel_elements:
                    data = self.elements_data[elem]
                    try:
                        elem_id = data['id']
                    except (KeyError, TypeError):
                        continue
                    self.anchored_elem_id_to_elem_name(data['raw'][-3], path, elem)
                pass

        # записываем привязки по элементам
        # for elem in self.elements_data:
        #     data = self.elements_data[elem]
        #     try:
        #         elem_id = data['id']
        #     except:
        #         continue
        #     if elem_id in self.anchored:
        #         for anchor in self.anchored[elem_id]:
        #             if anchor['anchor'] in self.elements_index:
        #                 # меняем идентификаторы на имена полей
        #                 anchor['anchor'] = self.elements_index[anchor['anchor']]
        #         data['anchored'] = self.anchored[elem_id]

        # return result
        except Exception as err:
            raise ExtException(parent=err)

    def anchored_elem_id_to_elem_name(self, elem_raw_data, path, current_elem):
        try:
            offset = 6
            # к чему привязан этот элемент
            for anchor_border in range(6):
                for j in range(1, 3):
                    elem_id = elem_raw_data[offset][j][1]
                    if int(elem_id) > 0:
                        try:
                            elem_raw_data[offset][j][1] = self.elements_index[elem_id][
                                                          len(path):]  # нужно имя относительно панели
                        except KeyError:  # если такого индекса нет, то это сам элемент - но это не точно
                            elem_raw_data[offset][j][1] = current_elem[len(path):]
                offset += 1
            # кто привязан к этому элементу
            for anchor_border in range(4):
                count_anchor = int(elem_raw_data[offset])
                for j in range(count_anchor):
                    offset += 1
                    anchor_data = elem_raw_data[offset]
                    elem_id = anchor_data[1]
                    if int(elem_id) > 0:
                        try:
                            elem_name = self.elements_index[elem_id][len(path):]
                            anchor_data[1] = elem_name
                        except KeyError:
                            pass  # ошибка, привязан элемент с другой страницы - в интерфейсе такой возможности нет

                # elem_raw_data[offset] = '0'
                # del elem_raw_data[offset + 1: offset + 1 + count_anchor]
                offset += 1
        except Exception as err:
            raise ExtException(parent=err)
        pass

    def decode_pages(self, path, raw_data):
        # def decode_anchored():
        #     self.anchored = {}
        #     type_offset = 2
        #     for border in range(1, 5):
        #         # anchored_elements = {}
        #         # self.anchored.append(anchored_elements)
        #         type_count = int(raw_data[type_offset])
        #         for elem in range(type_count):
        #             _elem_data = raw_data[type_offset + 1 + elem]
        #             self.add_anchor('$Panel', border, _elem_data)
        #             # _elem_id = _elem_data[1]
        #             #
        #             # if _elem_id not in self.anchored:
        #             #     self.anchored[_elem_id] = {}
        #             # if border not in self.anchored[_elem_id]:
        #             #     self.anchored[_elem_id][border] = []
        #             # self.anchored[_elem_id][border] += [['Panel', _elem_data[0], _elem_data[2]]]
        #         raw_data[type_offset] = '0'
        #         del raw_data[type_offset + 1: type_offset + 1 + type_count]
        #         type_offset += 1
        def delete_anchored():
            type_offset = 2
            for border in range(4):
                type_count = int(raw_data[type_offset])
                raw_data[type_offset] = '0'
                del raw_data[type_offset + 1: type_offset + 1 + type_count]
                type_offset += 1

        def decode_info():
            pages_info_offset = pages_offset + 4

            pages_info_count = int(raw_data[pages_info_offset])
            extra_page_info_count = pages_info_count - page_count * 4  # хрен его знает что это, но встречается большее количество записей
            # if page_count * 4 != pages_info_count:
            #     raise NotImplementedError()

            for i in range(page_count):
                offset = i * 4 + 1 + pages_info_offset
                if i == 0:
                    offset = 1 + pages_info_offset
                    page_info = raw_data[offset: offset + 4 + extra_page_info_count]
                else:
                    offset = i * 4 + 1 + pages_info_offset + extra_page_info_count
                    page_info = raw_data[offset: offset + 4]

                elem_id = self.calc_id(path, self.pages[i], None)
                self.elements_data[elem_id]['info'] = page_info

            del raw_data[pages_info_offset + 1: pages_info_offset + 1 + pages_info_count]
            raw_data[pages_info_offset] = 'в отдельном файле'

        pages_offset = self.pages_offset(raw_data)
        try:
            pages_raw_data = raw_data[pages_offset]
            format_version = pages_raw_data[0]
            if format_version != '1':
                print(f'Неизвестный формат страниц. {format_version} != 1')
            page_count = int(pages_raw_data[1])
            self.pages = []
            for i in range(page_count):
                raw_page = pages_raw_data[i + 2]
                page_format_version = raw_page[0]
                # if page_format_version not in ['3', '4']:
                #     print(f'Неизвестный формат страницы. {page_format_version} != 3')
                page_name = helper.str_decode(raw_page[6])
                if self.auto_include:
                    self.elements_tree.append({
                        "name": page_name,
                        "type": "Page",
                        "child": []
                    })
                elem_id = self.calc_id(path, page_name, None)

                self._elements_tree_index[elem_id] = len(self.elements_tree) - 1
                self.pages.append(page_name)

                self.elements_data[elem_id] = {
                    "ver": self.ver,
                    "page_format_version": page_format_version,
                    "raw": raw_page
                }
            del pages_raw_data[1:1 + 2 + page_count]
            self.elements_data[self.calc_id(path, '-pages-', None)] = self.pages

            decode_info()
            if self.auto_include:
                delete_anchored()

        except Exception as err:
            raise ExtException(parent=err)

    @staticmethod
    def pages_offset(raw_data):
        try:
            return calc_offset([[2, 1], [1, 1], [1, 1], [1, 1], [1, 1], [1, 1], [4, 0]], raw_data)
        except Exception as err:
            raise ExtException(message='Не смогли найти описание страниц элементов формы')

    def anchored_elem_name_to_elem_id(self, result, path):
        def anchor_name_is_num(name):
            try:
                return str(int(name)) == name
            except (TypeError, ValueError):
                return False

        try:
            for elem in result:
                elem_raw_data = elem[-3]
                elem_id = elem[1]
                offset = 6
                _path = path.replace('includr', 'include')
                for anchor_border in range(6):
                    border = 1 if anchor_border > 3 else anchor_border
                    for j in range(1, 3):
                        anchor_name = elem_raw_data[offset][j][1]
                        if anchor_name == '-1':  # ни к чему не привязан
                            continue
                        if anchor_name == '0':  # привязан к панели
                            anchor_border = int(elem_raw_data[offset][j][2])
                            anchor_border = 1 if anchor_border > 3 else anchor_border
                            self.anchor_index[anchor_border].append([elem_raw_data[offset][0], elem_id, str(border)])
                        if anchor_name_is_num(anchor_name):
                            continue
                        else:
                            elem_raw_data[offset][j][1] = self.elements_index[
                                _path + anchor_name]  # нужно имя относительно панели
                    offset += 1

                for anchor_border in range(4):
                    count_anchor = int(elem_raw_data[offset])
                    for j in range(count_anchor):
                        offset += 1
                        anchor_data = elem_raw_data[offset]
                        anchor_name = anchor_data[1]
                        if not anchor_name_is_num(anchor_name):
                            anchor_data[1] = self.elements_index[_path + anchor_name]
                    # elem_raw_data[offset] = '0'
                    # del elem_raw_data[offset + 1: offset + 1 + count_anchor]
                    offset += 1
        except Exception as err:
            raise ExtException(parent=err)
        pass


# --- MetaDataObject/Form/FormElements27/FormElements27.py ---
class FormElements27:
    FormProps = FormProps
    Panel = Panel

    def __init__(self, form):
        self.form = form
        self.props_index = {}
        self.last_elem_id = 1
        self.field_data_source = []

    @property
    def options(self):
        return self.form.options

    @property
    def auto_include(self):
        if self.form.options:
            return self.form.options.get('auto_include')
        return False

    @property
    def elements_data(self):
        return self.form.elements_data

    @property
    def elements_tree(self):
        return self.form.elements_tree

    def decode(self, src_dir, dest_dir, dest_path, raw_data):
        try:
            self.form.props = self.FormProps.decode_list(self.form, raw_data[2][2])
            self.form.elements_tree, self.form.elements_data = self.decode_elements(raw_data)
        except Exception as err:
            raise ExtException(parent=err)

    def decode_elements(self, form_data):
        try:
            meta_type = form_data[1][2][0]
            if meta_type != '09ccdc77-ea1a-4a6d-ab1c-3435eada2433':
                raise ExtException(message=f"Неизвестный формат элементов формы",
                                   detail=f"Новый тип элементов формы {meta_type}, "
                                          f"просьба передать файл формы {self.form.header.get('name')} разработчикам")
            self.create_prop_index_by_elem_id(form_data[2][3])

            elements_tree, elements_data, elements_id = self.Panel.decode(self, '', form_data[1][2])
            return elements_tree, elements_data
        except Exception as err:
            raise ExtException(parent=err)

    def create_prop_index_by_elem_id(self, raw_data):
        try:
            self.props_index = {}
            _props = {}
            if not self.form.props:
                return
            for prop in self.form.props:
                _props[prop['id']] = prop

            element_count = int(raw_data[0])
            if not element_count:
                return

            for i in range(element_count):
                elem_raw_data = raw_data[i + 1]
                elem_id = elem_raw_data[0]
                # if elem_raw_data[1][0] == '1':
                prop_id = elem_raw_data[1][1][0]
                try:
                    self.props_index[elem_id] = {'name': _props[prop_id]['name'], 'index': elem_raw_data[1]}
                except KeyError:
                    pass
                # else:
                #     raise NotImplementedError('prop index  > 1')
        except Exception as err:
            raise ExtException(parent=err)

    def create_prop_index_by_name(self):
        try:
            self.props_index = {}
            if self.form.props:
                for prop in self.form.props:
                    self.props_index[prop['name']] = prop['id']
        except Exception as err:
            raise ExtException(parent=err)

    def fill_datasource(self, raw_data):
        raw_data.append(str(len(self.field_data_source)))
        for elem_id, prop_id in self.field_data_source:
            raw_data.append(
                [str(elem_id), ['1', [str(prop_id)]]]
            )
