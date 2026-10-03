"""Парсер «скобкофайлов» 1С (текстовый формат {значение, {вложенный список}, ...}).

Порт decode-части v8unpack.json_container_decoder (MIT) — только разбор, без обратной записи.

Производительность: текущее значение собирается списком фрагментов и склеивается
один раз в _end_value; строковые прогоны добавляются срезом (регекс _RUN_PARAM,
str.find). Посимвольная конкатенация строки квадратична и на скобкофайлах форм
в десятки МБ (регламентированные отчёты) тормозит на порядки.

Две вещи дороже самого разбора (замер на УНФ: 54.3 млн строк, 1481 МиБ):
  * диспетчер строк. `getattr(self, f'_decode_line_{mode.name.lower()}')` на каждой
    строке — это f-строка, два новых строковых объекта и поиск атрибута; три
    режима, дающие 99% строк, теперь ветки if, getattr остался только редким;
  * посимвольный проход по строкам без кавычек (27% строк, но в них все значения
    метаданных). Их режет str.split — `_fast_param`; автомат остался для строк
    со строковыми значениями.
"""
import os
import re
from enum import Enum

from .ext_exception import ExtException

_RUN_PARAM = re.compile(r'[^,"}\n]+')


class Mode(Enum):
    READ_PARAM = 1
    BEGIN_READ_STRING_VALUE = 2
    BEGIN_READ_MULTI_STRING_VALUE = 6
    END_READ_MULTI_STRING_VALUE = 7
    END_READ_STRING_VALUE = 3
    READ_B64 = 4
    READ_TEXT_FILE = 5


# псевдонимы для сравнения по идентичности в диспетчере строк
_READ_PARAM = Mode.READ_PARAM
_BEGIN_STRING = Mode.BEGIN_READ_STRING_VALUE
_BEGIN_MULTI_STRING = Mode.BEGIN_READ_MULTI_STRING_VALUE


class JsonContainerDecoder:
    def __init__(self, *, src_dir=None, file_name=None):
        self.data = None
        self.raw_data = None
        self.mode = Mode.READ_PARAM
        self.current_object = None
        self._chunks = []
        self._value_is_none = True
        self.previous_char = None
        self.src_dir = src_dir
        self.file_name = file_name
        self.path = []
        self.params_in_line = 0
        self.line_number = None

    # ---------- аккумулятор текущего значения ----------

    def _value(self):
        return ''.join(self._chunks)

    def _value_is_64(self):
        return not self._value_is_none and len(self._chunks) and \
            sum(map(len, self._chunks)) == 64 and self._value()

    def _reset_value_empty(self):
        self._chunks = []
        self._value_is_none = False

    def _add_to_current_value(self, value):
        if self._value_is_none:
            self._chunks = [value]
            self._value_is_none = False
        else:
            self._chunks.append(value)
        self.previous_char = value

    # ---------- разбор ----------

    def decode_file(self, file):
        self.mode = _READ_PARAM
        self.data = []
        # счётчик строк живёт в локальной переменной: 54 млн записей в атрибут
        # на корпусе УНФ стоят секунд, а читают его только сообщения об ошибках
        line_number = 1
        for line in file:
            try:
                mode = self.mode
                if mode is _READ_PARAM:
                    self._decode_line_read_param(line)
                elif mode is _BEGIN_MULTI_STRING:
                    # продолжение многострочного строкового значения: 71% строк
                    # реального файла — текст модуля формы и пустые строки между
                    # ним, вся работа — один append
                    if '"' in line and line.count('""') * 2 != line.count('"'):
                        self.mode = _BEGIN_STRING
                        self.decode_object(line)
                    else:
                        self._chunks.append(line)
                elif mode is _BEGIN_STRING:
                    self.decode_object(line)
                else:
                    handler = getattr(self, f'_decode_line_{mode.name.lower()}')
                    handler(line)
                line_number += 1
            except BigBase64 as err:
                raise err from err
            except Exception as err:
                self.line_number = line_number
                raise ExtException(
                    parent=err,
                    message="Ошибка при разборе скобкофайла",
                    detail=f'{os.path.basename(self.src_dir)}/{self.file_name} проблема до строки {line_number}',
                    dump=dict(
                        mode=self.mode,
                        current_object=self.current_object,
                        path=self.path
                    ))
        self.line_number = line_number
        if self.mode != Mode.READ_PARAM:
            raise ExtException(
                message="Ошибка при разборе скобкофайла",
                detail=f'{os.path.basename(self.src_dir)}/{self.file_name} файл закончен в режиме {self.mode}, '
                       f'создайте проблему на github, укажите текст ошибки и приложите указанный файл'
            )
        if not self.data:
            return ''
        return self.data

    def decode_line(self, line):
        handler = getattr(self, f'_decode_line_{self.mode.name.lower()}')
        return handler(line)

    def _decode_line_read_b64(self, line):
        if line == '\n':
            return
        self.decode_b64_line(line, 0)

    def _decode_line_read_text_file(self, line):
        self.data += line

    def _decode_line_read_param(self, line):
        first = line[0]
        if first == '{':  # новый объект, исходим из того, что формат записи предполагает только один новый объект
            if self.current_object is None:
                self.current_object = []
                self.data.append(self.current_object)
                self.path.append(self.current_object)
            else:
                self.current_object.append([])
                self.current_object = self.current_object[-1]
                self.path.append(self.current_object)

            self._reset_value_empty()
            if line.startswith('{#base64'):
                # это скорее всего файл целиком из двоичных данных
                if len(self.data) == 1 and len(self.data[0]) == 2 and self.data[0][0] == '1':
                    raise BigBase64()
                self.mode = Mode.READ_B64
                self.decode_b64_line(line, 1)
            else:
                self._split_param_line(line, 1, False)
        elif first == '}':
            self._split_param_line(line, 1, True)
        elif first == '\n' and self._value_is_64():
            self.mode = Mode.READ_B64
            return
        else:
            if not self.data and self._value_is_none:
                if line == '\n':
                    return
                self.data = line
                self.mode = Mode.READ_TEXT_FILE
            elif self.data == [[]] and not self._value_is_none and self._value() == '':  # текстовый файл с json
                self.data = '{\n' + line
                self.mode = Mode.READ_TEXT_FILE
            elif self._value_is_64():  # строка base64 начавшаяся не с новой строки
                self.mode = Mode.READ_B64
                self._decode_line_read_b64(line)
            else:
                raise ExtException(
                    message='Неожиданное начало объекта',
                    detail=f'в файле :{self.src_dir}/{self.file_name}, path:{self.path})')

    def _split_param_line(self, line, start, close_first):
        """Строка режима READ_PARAM с позиции start: оптом до первого строкового значения.

        :param close_first: первым действием закрыть текущий объект — ветка `}`

        Значения без кавычек режет str.split (`_fast_param`), а с первой кавычки
        строку ведёт прежний посимвольный автомат: ему принадлежат строковые и
        многострочные значения, base64 и переходы режимов.
        """
        quote = line.find('"', start)
        if quote < 0:
            self._fast_param(line[start:], close_first)
        elif quote == start:
            if close_first:
                self._end_current_object()
            self.decode_object(line, quote)
        else:
            self._fast_param(line[start:quote], close_first)
            self.decode_object(line, quote)

    def _fast_param(self, body, close_first):
        """Часть строки без кавычек: значения и закрытия объектов нарезкой str.split.

        :param body: прогоны, `,` и `}` — до первого строкового значения или до
            конца строки
        :param close_first: первым действием закрыть текущий объект — ветка `}`

        Повторяет decode_object символ за символом, но прогоны между `,` и `}`
        режет на C-уровне. Состояние после строки то же: незакрытый пробег лежит
        в _chunks/_value_is_none, previous_char равен '}' только сразу после
        закрытия объекта (других сравнений с ним в парсере нет).
        """
        obj = self.current_object
        path = self.path
        if close_first:
            # _end_current_object: незакрытое значение — в объект, затем pop
            if self.previous_char != '}':
                obj.append(None if self._value_is_none else self._value())
            if path:  # могут быть лишние закрывающие скобки
                path.pop()
            obj = path[-1] if path else None
            self.current_object = obj
            pending = None
            is_none = True
            prev_close = True
        else:
            is_none = self._value_is_none
            pending = None if is_none else self._value()
            prev_close = self.previous_char == '}'

        if body.endswith('\n'):
            body = body[:-1]
        segments = body.split('}')
        last = len(segments) - 1
        for idx, seg in enumerate(segments):
            if seg:
                pieces = seg.split(',')
                for piece in pieces[:-1]:  # прогоны, завершённые запятой
                    if piece:
                        pending = piece if not pending else pending + piece
                        is_none = False
                        prev_close = False
                    if not prev_close:
                        obj.append(None if is_none else pending)
                        is_none = False
                    pending = ''
                    prev_close = False
                piece = pieces[-1]  # последний пробег запятой не завершён
                if piece:
                    pending = piece if not pending else pending + piece
                    is_none = False
                    prev_close = False
            if idx != last:  # за сегментом стоит '}'
                if not prev_close:
                    obj.append(None if is_none else pending)
                    is_none = False
                if path:
                    path.pop()
                obj = path[-1] if path else None
                pending = None
                is_none = True
                prev_close = True

        self.current_object = obj
        self._chunks = [] if pending is None else [pending]
        self._value_is_none = is_none
        self.previous_char = '}' if prev_close else ','

    def _decode_line_begin_read_string_value(self, line):
        self.decode_object(line)

    def decode_object(self, line, i=0):
        """Посимвольный разбор с позиции i: строковые значения и переходы режимов.

        Значения без кавычек до этой позиции уже забрал `_fast_param`, поэтому
        позиция передаётся снаружи, а не всегда нулевая.
        """
        n = len(line)
        while i < n:
            char = line[i]
            if self.mode == Mode.READ_PARAM:
                if char == ',':
                    self._end_value()
                    i += 1
                elif char == '}':
                    self._end_current_object()
                    i += 1
                elif char == '\n':
                    break
                elif char == '"':
                    if line.endswith(',"\n') and i == n - 2:
                        self.mode = Mode.BEGIN_READ_MULTI_STRING_VALUE
                        self._add_to_current_value(line[i:])
                        break
                    else:
                        self.mode = Mode.BEGIN_READ_STRING_VALUE
                        self._add_to_current_value(char)
                        i += 1
                else:
                    run = _RUN_PARAM.match(line, i)
                    self._add_to_current_value(run.group())
                    i = run.end()
            elif self.mode == Mode.BEGIN_READ_STRING_VALUE:
                quote = line.find('"', i)
                if quote < 0:
                    self._add_to_current_value(line[i:])
                    break
                if quote > i:
                    self._add_to_current_value(line[i:quote])
                self.mode = Mode.END_READ_STRING_VALUE
                self._add_to_current_value('"')
                i = quote + 1
            elif self.mode == Mode.END_READ_STRING_VALUE:
                if char == '"':
                    self.mode = Mode.BEGIN_READ_STRING_VALUE
                    self._add_to_current_value(char)
                elif char == ',':
                    self._end_value()
                elif char == '}':
                    self._end_current_object()
                else:
                    self._add_to_current_value(char)
                i += 1
            else:
                raise NotImplemented(f'mode {self.mode}')

    def decode_b64_line(self, line, start_pos):
        end_pos = line.find('}')
        if end_pos >= 0:
            self._chunks.append(line[start_pos:end_pos])
            if start_pos == 1:  # b64 не разбит на строки
                self._chunks.insert(0, '#')
            self._end_current_object()
            self.decode_object(line[end_pos + 1:])
            return True
        else:
            self._chunks.append(line[start_pos:-1])
            return False

    def _decode_line_begin_read_multi_string_value(self, line):
        if line.count('""') * 2 != line.count('"'):
            self.mode = Mode.BEGIN_READ_STRING_VALUE
            self.decode_object(line)
        else:
            self._chunks.append(line)
            return False

    def _decode_line_end_read_multi_string_value(self, line):
        if line[0] in ['{', '}']:
            self._end_value()
            self._decode_line_read_param(line)
        else:
            self.mode = Mode.BEGIN_READ_MULTI_STRING_VALUE
            self._chunks.append(",\n")
            self._decode_line_begin_read_multi_string_value(line)

    def _end_current_object(self):
        self._end_value()
        if self.path:  # могут быль лишние закрывающие скобки
            self.path.pop()
        self.current_object = self.path[-1] if self.path else None
        self._chunks = []
        self._value_is_none = True
        self.previous_char = '}'

    def _end_value(self):
        self.mode = Mode.READ_PARAM
        if self.previous_char != '}':
            # эквивалентность v8unpack: пустое значение после закрытого объекта
            # (например, «,,») даёт None, а не ''
            self.current_object.append(None if self._value_is_none else self._value())
            self._reset_value_empty()
        self.previous_char = ','


class BigBase64(Exception):
    pass
