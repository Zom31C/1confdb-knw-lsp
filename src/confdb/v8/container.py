# -*- coding: utf-8 -*-
"""Формат контейнеров 1С (32-битный и 64-битный): чтение оглавления и файлов.

Порт read-части v8unpack.container (MIT) — только чтение, без сборки.
"""
import collections
import os
import zlib
from datetime import datetime, timedelta
from struct import pack, unpack, calcsize

from .container_doc import Document
from .helper import clear_dir

Header = collections.namedtuple('Header', 'first_empty_block_offset, default_block_size, count_files')
Block = collections.namedtuple('Block', 'doc_size, current_block_size, next_block_offset, data')
File = collections.namedtuple('File', 'name, size, created, modified, data')
DocumentData = collections.namedtuple('DocumentData', 'size, data')


class Container:
    end_marker = 0x7fffffff
    header_fmt = '<4i'
    header_size = 16
    block_header_fmt = '<2s8s1s8s1s8s1s2s'
    block_header_size = 31
    block_header_fmt_size = 8
    index_fmt = 'i'
    default_block_size = 0x200
    index_block_size = 0x200

    def __init__(self):
        self.file = None
        self.offset = 0
        self.first_empty_block_offset = None
        self.default_block_size = 0x200
        self.files = None
        self.size = 0
        self.toc = []

    def read(self, file, offset=0):
        self.offset = offset
        try:
            header = self.read_header(file)
        except Exception as err:
            raise err from err

        if header.default_block_size == 0:
            raise BufferError('Container is empty')

        self.file = file
        self.first_empty_block_offset = header.first_empty_block_offset
        self.default_block_size = header.default_block_size
        #: Список файлов в контейнере
        self.files = self.read_files(self.file)

    def extract(self, dest_dir, deflate=False, recursive=False, *, progress=None):
        """
        Распаковывает содержимое контейнера в каталог

        :param dest_dir: каталог распаковки
        :type dest_dir: string
        :param deflate: разархивировать содержимое файлов
        :type deflate: bool
        :param recursive: выполнять рекурсивно
        :type recursive: bool
        """

        clear_dir(dest_dir)
        if not self.files:
            print('Пустой контейнер = распаковывать нечего')
            return

        for filename, file_obj in self.files.items():
            self.extract_file(filename, file_obj, dest_dir, deflate, recursive)

    @staticmethod
    def extract_file(filename, file_obj, path, deflate=False, recursive=False):
        file_path = os.path.join(path, filename)
        with open(file_path, 'wb') as f:
            if deflate:
                # wbits = -15 т.к. у архивированных файлов нет заголовков
                decompressor = zlib.decompressobj(-15)
                for chunk in file_obj.data:
                    decomressed_chunk = decompressor.decompress(chunk)
                    f.write(decomressed_chunk)
            else:
                for chunk in file_obj.data:
                    f.write(chunk)

        if not recursive:
            return

        # Каждый файл внутри контейнера может быть контейнером
        # Для проверки является ли файл контейнером проверим первые 4 бита
        # Способ проверки ненадежный - нужно придумать что-то другое
        file_is_container = False
        with open(file_path, 'rb') as f:
            if f.read(4) == b'\xFF\xFF\xFF\x7F':
                file_is_container = True
        if file_is_container:
            temp_name = file_path + '.tmp'
            os.rename(file_path, temp_name)
            with open(temp_name, 'rb') as f:
                _container = Container()
                _container.read(f)
                _container.extract(file_path, recursive=True)
            os.remove(temp_name)

    def read_header(self, file):
        """
        Считывыет заголовок контейнера.

        :param file: объект файла контейнера
        :type file: BufferedReader
        :return: Заголовок контейнера
        :rtype: Header
        """
        file.seek(0 + self.offset)
        buff = file.read(calcsize(self.header_fmt))
        header = unpack(self.header_fmt, buff)
        # if header[0] != self.end_marker:
        #     raise Exception('Bad container format')
        return Header(header[0], header[1], header[2])

    @classmethod
    def parse_datetime(cls, time):
        """
        Преобразует внутренний формат хранения дат файлов в контейнере в обычную дату

        :param time: внутреннее представление даты
        :type time: string
        :return: дата/время
        :rtype: datetime
        """
        # TODO проверить работу на *nix, т.к там начало эпохи - другая дата
        try:
            return datetime(1, 1, 1) + timedelta(microseconds=(time * 100))
        except OverflowError:
            return datetime(9999, 12, 31, 23, 59, 59)

    def read_files(self, file):
        """
        Считывает оглавление контейнера

        :param file: объект файла контейнера
        :type file: BufferedReader
        :return: словарь файлов в контейнере
        :rtype: OrderedDict
        """
        size = 0
        # Первый документ после заголовка содержит оглавление
        doc = Document(self)
        doc_data = doc.read(file, self.header_size, self.index_block_size)
        table_of_contents = [unpack(f'<2{self.index_fmt}', x) for x in
                             doc_data.split(pack(f'<{self.index_fmt}', self.end_marker))[:-1]]
        self.size += self.header_size + doc.full_size

        files = collections.OrderedDict()
        for file_description_offset, file_data_offset in table_of_contents:
            doc = Document(self)
            doc_data = doc.read(file, file_description_offset)
            self.size += doc.full_size
            fmt = ''.join(['<QQi', str(doc.data_size - calcsize('<QQi')), 's'])
            file_description = unpack(fmt, doc_data)
            # Из описания формата длина имени файла определяется точно, поэтому, теоретически, мусора быть не должно
            # По факту имя часто имеет в конце мусор, который чаще всего состоит из последовательности \x00 n-раз,
            # но иногда бывают и другие символы после \x00. Поэтому применяем вот такой костыль:
            name = file_description[3].decode('utf-16-le').partition('\x00')[0]

            doc = Document(self)
            file_data = doc.read_chunk(file, file_data_offset, self.default_block_size)
            self.size += doc.full_size

            inner_file = File(name, doc.data_size, self.parse_datetime(file_description[0]),
                              self.parse_datetime(file_description[1]), file_data)

            files[inner_file.name] = inner_file
        return files


class Container64(Container):
    end_marker = 0xffffffffffffffff  # 18446744073709551615
    header_fmt = '<1Q3i'
    header_size = 20
    block_header_fmt = '<2s16s1s16s1s16s1s2s'
    block_header_fmt_size = 16
    block_header_size = 55
    index_fmt = 'Q'
    index_block_size = 0x10000
