# -*- coding: utf-8 -*-
"""Чтение документов и блоков из контейнеров 1С.

Порт read-части v8unpack.container_doc (MIT) — только чтение.
"""
import collections
import math
from struct import unpack

Header = collections.namedtuple('Header', 'first_empty_block_offset, default_block_size, count_files')
Block = collections.namedtuple('Block', 'doc_size, current_block_size, next_block_offset, data')
File = collections.namedtuple('File', 'name, size, created, modified, data')
DocumentData = collections.namedtuple('DocumentData', 'size, data')


class Document:
    def __init__(self, container):
        self.container = container
        self.full_size = 0  # include all header size
        self.data_size = 0

    def read(self, file, offset, min_block_size=0):
        document_data = self.read_chunk(file, offset, min_block_size)
        return b''.join([chunk for chunk in document_data])

    def read_chunk(self, file, offset, min_block_size=0):
        """
        Считывает документ из контейнера. В качестве данных документа возвращается генератор.

        :param file: объект файла контейнера
        :type file: BufferedReader
        :param offset: смещение документа в контейнере
        :type offset: int
        :return: данные документа
        :rtype:
        """
        gen = self._read_gen(file, offset, min_block_size)

        try:
            self.data_size = next(gen)
        except StopIteration:
            self.data_size = 0

        return gen

    def _read_gen(self, file, offset, min_block_size=0):
        """
        Создает генератор чтения данных документа в контейнере.
        Первое значение генератора - размер документа (байт).
        Остальные значения - данные блоков, составляющих документ

        :param file: объект файла контейнера
        :type file: BufferedReader
        :param offset: смещение документа в контейнере (байт)
        :type offset: int
        :return: генератор чтения данных документа
        """
        header_block = self.read_block(file, offset)
        doc_size = max(header_block.doc_size, min_block_size)
        if doc_size > header_block.current_block_size:
            self.full_size = doc_size + math.ceil(
                header_block.doc_size / header_block.current_block_size) * min_block_size
            if min_block_size == self.container.index_block_size:
                self.full_size += self.container.index_block_size
        else:
            self.full_size = doc_size + self.container.block_header_size

        if header_block is None:
            return
        else:
            yield header_block.doc_size
            yield header_block.data

            left_bytes = header_block.doc_size - len(header_block.data)
            next_block_offset = header_block.next_block_offset

            while left_bytes > 0 and next_block_offset != self.container.end_marker:
                block = self.read_block(file, next_block_offset, left_bytes)
                left_bytes -= len(block.data)
                yield block.data
                next_block_offset = block.next_block_offset

    def read_block(self, file, offset, max_data_length=None):
        """
        Считывает блок данных из контейнера.

        :param file: объект файла контейнера
        :type file: BufferedReader
        :param offset: смещение блока в файле контейнера (байт)
        :type offset: int
        :param max_data_length: максимальный размер считываемых данных из блока (байт)
        :type max_data_length: int
        :return: объект блока данных
        :rtype: Block
        """
        file.seek(offset + self.container.offset)
        header_size = self.container.block_header_size
        buff = file.read(header_size)
        if not buff:
            return
        header = unpack(self.container.block_header_fmt, buff)

        doc_size = int(header[1], 16)
        current_block_size = int(header[3], 16)
        next_block_offset = int(header[5], 16)

        if max_data_length is None:
            max_data_length = min(current_block_size, doc_size)

        data_size = min(current_block_size, max_data_length)

        data = file.read(data_size)

        return Block(doc_size, current_block_size, next_block_offset, data)
