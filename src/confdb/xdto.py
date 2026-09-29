"""Разбор содержимого пакета XDTO из файла XDTOPackage.bin дампа стадии 3.

Файл пакета — это открытый XML (UTF-8 с BOM, без объявления <?xml?>), поэтому
состав пакета извлекается обычным разбором, без реверса бинарного формата.
Структура подтверждена на всех 334 пакетах реальной конфигурации (УНФ):
корень всегда `<package targetNamespace=…>`, его дети — `<import>`,
`<objectType>`, `<valueType>` и `<property>` (свойства уровня пакета), внутри
типов — `<property>`, `<typeDef>`, `<enumeration>` и `<pattern>`; целевое
пространство имён в XML совпадает со значением в header_json пакета.

Значения `type` и `base` хранятся как в XML (`xs:string`, `d2p1:БазовыйТовар`),
а пространство имён префикса разрешается отдельно — по объявлениям `xmlns:`
этого же файла. Объявления собираются по всему файлу, а не по области видимости
элемента: в пакете один префикс соответствует одному импортированному
пространству имён, а неразрешённый префикс даёт None, а не догадку.

Атрибуты, которым не досталось собственной колонки (ограничения простых типов,
localName, ref, default и т.п.), складываются в одну строку `facets`/`extra` —
это часть контракта, терять её нельзя, а колонок на каждый фасет много.
Известные пробелы (замерено на УНФ): `typeDef`, вложенный в `valueType`
(20 из 26315 типов), не извлекается — у простого типа нет свойства-владельца,
через которое такой тип хранится; флаги пакета elementFormQualified и
attributeFormQualified не сохраняются.
"""
import re
import xml.etree.ElementTree as ET

from .v8 import helper

# пространство имён по умолчанию в файле пакета XDTO
XDTO_NS = 'http://v8.1c.ru/8.1/xdto'

# объявления пространств имён: xmlns:d2p1="http://…"
RE_XMLNS = re.compile(r'xmlns:([\w.\-]+)\s*=\s*"([^"]*)"')

# объявления типов: objectType — именованный, typeDef — анонимный внутри свойства
TYPE_TAGS = ('objectType', 'typeDef', 'valueType')

TYPE_FACETS = ('variety', 'open', 'ordered', 'sequenced', 'abstract', 'mixed',
               'memberTypes', 'itemType', 'length', 'minLength', 'maxLength',
               'totalDigits', 'fractionDigits', 'minInclusive', 'maxInclusive',
               'minExclusive', 'maxExclusive', 'whiteSpace', 'localName',
               'elementFormQualified', 'attributeFormQualified')

PROP_FACETS = ('base', 'ref', 'default', 'fixed', 'localName', 'variety',
               'open', 'ordered', 'sequenced', 'abstract', 'length',
               'minLength', 'maxLength', 'totalDigits', 'fractionDigits',
               'minInclusive', 'maxInclusive', 'minExclusive', 'maxExclusive',
               'whiteSpace', 'memberTypes', 'itemType', 'mixed')


def _local(tag):
    """'{http://v8.1c.ru/8.1/xdto}objectType' -> 'objectType'."""
    return tag.rsplit('}', 1)[-1]


def _int(value):
    """'0' -> 0, '-1' -> -1, всё остальное -> None."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def split_qname(value, prefixes):
    """'d2p1:Имя' -> ('Имя', namespace префикса или None); без префикса — как есть."""
    if not value or ':' not in value:
        return value, None
    prefix, _, name = value.partition(':')
    return name, prefixes.get(prefix)


def _facets(el, names):
    """Прочие атрибуты элемента строкой 'имя=значение; …' или None.

    Порядок фиксированный (сначала перечисленные в names, затем атрибуты с
    пространством имён по алфавиту) — строку сравнивают между базами.
    """
    parts = [f'{key}={el.get(key)}' for key in names if el.get(key)]
    qualified = sorted(key for key in el.attrib if '}' in key and el.attrib[key])
    parts += [f'{key.rsplit("}", 1)[-1]}={el.attrib[key]}' for key in qualified]
    return '; '.join(parts) or None


def _patterns(el):
    """Строки 'pattern=…' по детям <pattern> (регулярные выражения значения)."""
    out = []
    for child in el:
        if _local(child.tag) == 'pattern':
            text = (child.text or '').strip()
            if text:
                out.append(f'pattern={text}')
    return out


def _enumerations(el):
    """Допустимые значения типа-перечисления: текст детей <enumeration>."""
    values = [(child.text or '').strip() for child in el
              if _local(child.tag) == 'enumeration']
    return ' | '.join(v for v in values if v) or None


def _property(el, prefixes):
    """Свойство: атрибуты XML плюс вложенный анонимный тип, если он есть."""
    prop = {
        'name': el.get('name'),
        'type': el.get('type'),
        'type_ns': None,
        'lower_bound': _int(el.get('lowerBound')),
        'upper_bound': _int(el.get('upperBound')),
        'nillable': 1 if el.get('nillable') == 'true' else 0,
        'form': el.get('form'),
        'extra': _facets(el, PROP_FACETS),
        'nested': None,
    }
    if prop['type']:
        prop['type_ns'] = split_qname(prop['type'], prefixes)[1]
    for child in el:
        if _local(child.tag) in ('objectType', 'typeDef'):
            prop['nested'] = _type(child, prefixes)
            break
    return prop


def _type(el, prefixes):
    """Тип пакета: имя, базовый тип, фасеты, значения перечисления, свойства."""
    base = el.get('base')
    facets = _facets(el, TYPE_FACETS)
    patterns = _patterns(el)
    if patterns:
        facets = '; '.join(([facets] if facets else []) + patterns)
    return {
        'name': el.get('name'),
        'kind': _local(el.tag),
        'base': base,
        'base_ns': split_qname(base, prefixes)[1] if base else None,
        'facets': facets,
        'values': _enumerations(el),
        'props': [_property(child, prefixes) for child in el
                  if _local(child.tag) == 'property'],
    }


def parse_package(path):
    """Состав пакета XDTO: {'namespace', 'imports', 'types', 'props'} или None.

    'props' — свойства, объявленные прямо в пакете (вне типов). None — файл не
    является пакетом XDTO (другой бинарный макет, битый XML): вызывающий код
    тогда не пишет ничего, а не пустые таблицы.
    """
    try:
        with open(helper.long_path(path), 'rb') as f:
            # проверка по началу файла: макеты (.mxl и т.п.) бывают большими,
            # читать их целиком ради ответа «это не пакет XDTO» незачем
            if not f.read(64).lstrip(b'\xef\xbb\xbf \t\r\n').startswith(b'<package'):
                return None
            f.seek(0)
            raw = f.read()
    except OSError:
        return None
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None
    if _local(root.tag) != 'package':
        return None
    # объявления xmlns: из текста: ElementTree их не сохраняет, а они нужны,
    # чтобы отличить тип из импортированного пакета от встроенного типа XSD
    prefixes = {m.group(1): m.group(2)
                for m in RE_XMLNS.finditer(raw.decode('utf-8-sig', 'replace'))}
    imports = [el.get('namespace') for el in root
               if _local(el.tag) == 'import' and el.get('namespace')]
    return {'namespace': root.get('targetNamespace'),
            'imports': imports,
            'types': [_type(el, prefixes) for el in root
                      if _local(el.tag) in TYPE_TAGS],
            'props': [_property(el, prefixes) for el in root
                      if _local(el.tag) == 'property']}
