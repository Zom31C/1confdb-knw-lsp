"""Разбор модуля 1С (.bsl) на процедуры и функции.

Извлекает для каждого метода: вид (процедура/функция), имя, сигнатуру,
признак Экспорт, директивы (&НаКлиенте, &НаСервере, &Вместо и т.п.),
описание (блок комментариев непосредственно над методом, без пустой строки
между ним и сигнатурой — хранится как есть, вместе с //), номера строк и
тело строго с объявления по КонецПроцедуры/КонецФункции.

Текст модуля (первое возвращаемое значение) — модуль как есть минус код
методов: у каждого метода удалены строки от следующей за сигнатурой по
КонецПроцедуры/КонецФункции. Комментарии, препроцессор (#Если, #Область …),
пустые строки, директивы и сигнатуры сохраняются; подстановка method.body
вместо каждой сигнатуры восстанавливает исходный модуль (с точностью до
завершающих пустых строк в конце файла). В незакрытых методах (битый
источник без КонецПроцедуры/КонецФункции) тело тянется до конца модуля.
"""
import re

RE_DIRECTIVE = re.compile(r'^\s*&[A-Za-zА-Яа-яЁё_]+')
RE_METHOD = re.compile(
    r'^\s*(?P<kind>Процедура|Функция)\s+(?P<name>[A-Za-zА-Яа-яЁё_0-9]+)\s*\(', re.IGNORECASE)
RE_METHOD_END = re.compile(r'^\s*Конец(?:Процедуры|Функции)', re.IGNORECASE)
RE_EXPORT = re.compile(r'Экспорт', re.IGNORECASE)
RE_COMMENT = re.compile(r'^\s*//')


def _collapse(text):
    return ' '.join(text.split())


def parse_methods(text):
    """Разбирает текст модуля. Возвращает (текст модуля без кода методов, методы).

    Метод — словарь: kind ('процедура'/'функция'), name, signature, is_export,
    directives (список строк директив), description (блок комментариев
    непосредственно над методом, как есть), line_start (строка сигнатуры),
    line_end (строка Конец…), body (сигнатура..Конец… включительно).
    """
    lines = text.splitlines()
    methods = []
    pending = []
    cuts = []

    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]

        if RE_DIRECTIVE.match(line):
            pending.append(line.strip())
            i += 1
            continue

        m = RE_METHOD.match(line)
        if m:
            # описание: комментарии непосредственно над сигнатурой
            # (директивы между ними прозрачны); пустая строка разрывает связь
            j = i - 1
            while j >= 0 and RE_DIRECTIVE.match(lines[j]):
                j -= 1
            k2 = j
            while k2 >= 0 and RE_COMMENT.match(lines[k2].strip()):
                k2 -= 1
            description = '\n'.join(lines[k2 + 1:j + 1])
            # сигнатура может тянуться на несколько строк — балансируем скобки
            header = [line]
            depth = line.count('(') - line.count(')')
            t = i
            while depth > 0 and t + 1 < n:
                t += 1
                header.append(lines[t])
                depth += lines[t].count('(') - lines[t].count(')')
            header_text = ' '.join(header)
            sig = header_text[header_text.index('(') + 1:]
            paren = sig.rfind(')')
            tail = sig[paren + 1:] if paren >= 0 else ''
            sig = sig[:paren] if paren >= 0 else sig
            # тело до парного КонецПроцедуры/КонецФункции
            k = t
            nest = 1
            while k + 1 < n and nest:
                k += 1
                if RE_METHOD.match(lines[k]):
                    nest += 1
                elif RE_METHOD_END.match(lines[k]):
                    nest -= 1
            methods.append({
                'kind': 'процедура' if m.group('kind').lower() == 'процедура' else 'функция',
                'name': m.group('name'),
                'signature': _collapse(sig),
                'is_export': bool(RE_EXPORT.search(tail)),
                'directives': list(pending),
                'description': description,
                'line_start': i + 1,
                'line_end': k + 1,
                'body': '\n'.join(lines[i:k + 1]),
            })
            cuts.append((t + 1, k))
            pending = []
            i = k + 1
            continue

        s = line.strip()
        if pending and s and not RE_COMMENT.match(s):
            pending = []
        i += 1

    out = []
    ci = 0
    for idx, ln in enumerate(lines):
        if ci < len(cuts) and idx > cuts[ci][1]:
            ci += 1
        if ci < len(cuts) and cuts[ci][0] <= idx <= cuts[ci][1]:
            continue
        out.append(ln)
    return '\n'.join(out), methods
