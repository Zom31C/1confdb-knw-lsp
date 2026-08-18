"""Проверка запросов языка 1С:Предприятие (лексер, парсер, семантический контроль).

Синтаксис — подмножество языка запросов 1С, достаточное для реальных запросов
СКД (сверено с официальным синтаксис-помощником платформы: shquery_ru.hbk и
его HTML-изданием, каталог query соседнего проекта 1c-syntax-db-extractor):
ПОМЕСТИТЬ, ОБЪЕДИНИТЬ [ВСЕ], соединения, вложенные запросы, параметры &…,
ВЫБОР (с операндом и без — операндная форма встречается в реальных запросах,
хотя в таблице грамматики справки показана только форма с КОГДА), ВЫРАЗИТЬ,
виртуальные таблицы с аргументами, ИНДЕКСИРОВАТЬ ПО, УПОРЯДОЧИТЬ/ПОРЯДОК
(в т.ч. ИЕРАРХИЯ [УБЫВ]), АВТОУПОРЯДОЧИВАНИЕ, ИТОГИ (ПО/ОБЩИЕ, [ТОЛЬКО]
ИЕРАРХИЯ, ПЕРИОДАМИ(…), несколько ИТОГИ подряд), СГРУППИРОВАТЬ ПО (в т.ч.
ГРУППИРУЮЩИМ НАБОРАМ — с вложенными скобками ((…), (…)) и плоским списком),
В [ИЕРАРХИИ] / НЕ В, НЕ МЕЖДУ, НЕ ПОДОБНО, ПОДОБНО … [СПЕЦСИМВОЛ …],
ЕСТЬ [НЕ] NULL, ССЫЛКА, псевдонимы с КАК и без, необязательные области СКД
в фигурных скобках {…}, ДЛЯ ИЗМЕНЕНИЯ [[OF] <таблицы>].

Семантический контроль опирается на метаданные в БД: существование таблиц
(объектов и виртуальных таблиц), существование полей (meta_attribute +
стандартные поля), цепочки разыменования ссылок (attribute_ref).
"""
import re

RE_IDENT = re.compile(r'[A-Za-zА-Яа-яЁё_][A-Za-zА-Яа-яЁё_0-9]*')
RE_NUM = re.compile(r'\d+(?:\.\d+)?')
RE_STR = re.compile(r"'(?:[^'\n]|'')*'")
RE_STR2 = re.compile(r'"(?:[^"\n]|"")*"')
RE_PARAM = re.compile(r'&[A-Za-zА-Яа-яЁё_0-9]*')
RE_OP = re.compile(r'<=|>=|<>|[-+*/%=<>,().;{}]')


class QueryError(ValueError):
    """Ошибка разбора запроса с позицией."""

    def __init__(self, message, pos=None):
        super().__init__(message if pos is None else f'{message} (поз. {pos})')
        self.pos = pos


def tokenize(text):
    """Список токенов (kind, value, pos); kind: id/num/str/param/op."""
    tokens = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in ' \t\r\n':
            i += 1
            continue
        if text.startswith('//', i):
            j = text.find('\n', i)
            i = n if j < 0 else j + 1
            continue
        m = RE_STR.match(text, i) or RE_STR2.match(text, i)
        if m:
            tokens.append(('str', m.group(0)[1:-1].replace(m.group(0)[0] * 2, m.group(0)[0]), i))
            i = m.end()
            continue
        m = RE_PARAM.match(text, i)
        if m and (i == 0 or not text[i - 1].isalnum()):
            tokens.append(('param', m.group(0)[1:], i))
            i = m.end()
            continue
        m = RE_IDENT.match(text, i)
        if m:
            tokens.append(('id', m.group(0), i))
            i = m.end()
            continue
        m = RE_NUM.match(text, i)
        if m:
            tokens.append(('num', m.group(0), i))
            i = m.end()
            continue
        m = RE_OP.match(text, i)
        if m:
            tokens.append(('op', m.group(0), i))
            i = m.end()
            continue
        raise QueryError(f'Недопустимый символ {ch!r}', i)
    tokens.append(('eof', '', n))
    return tokens


_KW = {
    'ВЫБРАТЬ', 'РАЗЛИЧНЫЕ', 'РАЗРЕШЕННЫЕ', 'ПЕРВЫЕ', 'КАК', 'ИЗ', 'ГДЕ',
    'ПОРЯДОК', 'УПОРЯДОЧИТЬ', 'ПО', 'ВОЗР', 'УБЫВ', 'СГРУППИРОВАТЬ',
    'СГРУППИРОВАНО', 'ИМЕЮЩИЕ',
    'ОБЪЕДИНИТЬ', 'ВСЕ', 'ВЫБОР', 'КОГДА', 'ТОГДА', 'ИНАЧЕ', 'КОНЕЦ', 'И', 'ИЛИ',
    'НЕ', 'В', 'МЕЖДУ', 'ЕСТЬ', 'NULL', 'ПОДОБНО', 'СПЕЦСИМВОЛ', 'ПОМЕСТИТЬ',
    'ВЫРАЗИТЬ',
    'ЛЕВОЕ', 'ПРАВОЕ', 'ВНУТРЕННЕЕ', 'ПОЛНОЕ', 'ВНЕШНЕЕ', 'СОЕДИНЕНИЕ',
    'ИНДЕКСИРОВАТЬ', 'ИТОГИ', 'ИСТИНА', 'ЛОЖЬ', 'НЕОПРЕДЕЛЕНО', 'ССЫЛКА',
    'ГРУППИРУЮЩИМ', 'НАБОРАМ', 'ИЕРАРХИЯ', 'ИЕРАРХИИ', 'АВТОУПОРЯДОЧИВАНИЕ',
    'ТОЛЬКО', 'ПЕРИОДАМИ', 'ОБЩИЕ', 'ПУСТАЯТАБЛИЦА', 'ДЛЯ', 'ИЗМЕНЕНИЯ', 'OF',
    'СЕКУНДА', 'МИНУТА', 'ЧАС', 'ДЕНЬ', 'НЕДЕЛЯ', 'МЕСЯЦ', 'КВАРТАЛ', 'ГОД',
    'ДЕКАДА', 'ПОЛУГОДИЕ',
}

_JOINS = {
    'ЛЕВОЕ': 'left', 'ПРАВОЕ': 'right', 'ВНУТРЕННЕЕ': 'inner', 'ПОЛНОЕ': 'full',
}


class _Parser:
    def __init__(self, text):
        self.text = text
        self.tokens = tokenize(text)
        self.i = 0

    # -- низкие помощники -------------------------------------------------
    def peek(self, k=0):
        return self.tokens[min(self.i + k, len(self.tokens) - 1)]

    def next(self):
        tok = self.tokens[self.i]
        if tok[0] != 'eof':
            self.i += 1
        return tok

    def error(self, message):
        return QueryError(message, self.peek()[2])

    def is_kw(self, *words):
        tok = self.peek()
        return tok[0] == 'id' and tok[1].upper() in words

    def accept_kw(self, *words):
        if self.is_kw(*words):
            return self.next()
        return None

    def expect_kw(self, *words):
        tok = self.accept_kw(*words)
        if tok is None:
            raise self.error(f"Ожидалось {'/'.join(words)}, найдено {self.peek()[1]!r}")
        return tok

    def accept_op(self, op):
        tok = self.peek()
        if tok[0] == 'op' and tok[1] == op:
            return self.next()
        return None

    def expect_op(self, op):
        tok = self.accept_op(op)
        if tok is None:
            raise self.error(f"Ожидался {op!r}, найдено {self.peek()[1]!r}")
        return tok

    def expect_num(self):
        tok = self.peek()
        if tok[0] != 'num':
            raise self.error(f'Ожидалось число, найдено {tok[1]!r}')
        self.next()
        return tok[1]

    # -- скрипт и операторы ------------------------------------------------
    def parse_script(self):
        statements = [self.parse_statement()]
        while self.accept_op(';'):
            if self.peek()[0] == 'eof':
                break
            statements.append(self.parse_statement())
        if self.peek()[0] != 'eof':
            raise self.error(f'Лишний текст после запроса: {self.peek()[1]!r}')
        return statements

    def parse_statement(self):
        if self.accept_kw('УНИЧТОЖИТЬ'):
            return {'destroy': self.next()[1]}
        return self.parse_select()

    def parse_select(self):
        self.expect_kw('ВЫБРАТЬ')
        distinct = False
        while True:  # РАЗРЕШЕННЫЕ/РАЗЛИЧНЫЕ в произвольном порядке
            if self.accept_kw('РАЗЛИЧНЫЕ'):
                distinct = True
            elif self.accept_kw('РАЗРЕШЕННЫЕ'):
                continue
            else:
                break
        first = None
        if self.accept_kw('ПЕРВЫЕ'):
            first = self.expect_num()
        items = [self.parse_select_item()]
        while self.accept_op(','):
            items.append(self.parse_select_item())
        node = {'select': True, 'distinct': distinct, 'first': first, 'items': items,
                'put': None, 'source': None, 'where': None, 'group': None,
                'having': None, 'order': None, 'totals': None, 'union': []}
        self.parse_braced()
        if self.accept_kw('ПОМЕСТИТЬ'):
            node['put'] = self.next()[1]
        # clauses в произвольном порядке + необязательные области {…}
        while True:
            self.parse_braced()
            if self.accept_kw('ИЗ'):
                node['source'] = self.parse_sources()
            elif self.accept_kw('ГДЕ'):
                node['where'] = self.parse_expr()
            elif self.accept_kw('СГРУППИРОВАТЬ'):
                self.expect_kw('ПО')
                if self.accept_kw('ГРУППИРУЮЩИМ'):
                    # СГРУППИРОВАТЬ ПО ГРУППИРУЮЩИМ НАБОРАМ — допустимы и
                    # вложенные скобки ((…), (…)), и плоский список (…, …)
                    self.expect_kw('НАБОРАМ')
                    self.expect_op('(')
                    sets = []
                    while True:
                        if self.accept_op('('):
                            sets += self.parse_expr_list()
                            self.expect_op(')')
                        else:
                            sets.append(self.parse_expr())
                        if not self.accept_op(','):
                            break
                    self.expect_op(')')
                    node['group'] = sets
                else:
                    node['group'] = self.parse_expr_list()
            elif self.is_kw('СГРУППИРОВАНО'):
                # каноническое ключевое слово 1С — СГРУППИРОВАТЬ ПО
                raise self.error('в языке запросов 1С используется '
                                 'СГРУППИРОВАТЬ ПО, а не СГРУППИРОВАНО')
            elif self.accept_kw('ИМЕЮЩИЕ'):
                node['having'] = self.parse_expr()
            elif self.accept_kw('ИТОГИ'):
                node['totals'] = self.parse_totals()
            elif self.accept_kw('ПОРЯДОК', 'УПОРЯДОЧИТЬ'):
                self.expect_kw('ПО')
                node['order'] = self.parse_order()
            elif self.accept_kw('ИНДЕКСИРОВАТЬ'):
                self.expect_kw('ПО')
                self.parse_expr_list()
            elif self.accept_kw('АВТОУПОРЯДОЧИВАНИЕ'):
                node['autoorder'] = True
            elif self.accept_kw('ДЛЯ'):
                self.expect_kw('ИЗМЕНЕНИЯ')
                if self.accept_kw('OF'):
                    # ДЛЯ ИЗМЕНЕНИЯ [OF <Список таблиц верхнего уровня>]
                    while True:
                        self.next()
                        while self.accept_op('.'):
                            self.next()
                        if not self.accept_op(','):
                            break
            elif self.is_kw('ОБЪЕДИНИТЬ'):
                self.next()
                union_all = bool(self.accept_kw('ВСЕ'))
                node['union'].append((union_all, self.parse_select()))
                break
            else:
                break
        return node

    def parse_braced(self):
        """Необязательные области СКД: { … } с фрагментом запроса внутри."""
        while self.accept_op('{'):
            self._braced_block()

    def _braced_block(self):
        while True:
            tok = self.peek()
            if tok[0] == 'op' and tok[1] == '}':
                self.next()
                return
            if self._try_join():
                continue
            if self.accept_kw('ВЫБРАТЬ', 'ГДЕ', 'ХАРАКТЕРИСТИКИ', 'ВИДЫХАРАКТЕРИСТИК',
                              'ЗНАЧЕНИЯХАРАКТЕРИСТИК', 'ПОЛЕКЛЮЧА', 'ПОЛЕИМЕНИ',
                              'ПОЛЕТИПАЗНАЧЕНИЯ'):
                continue
            self.parse_fragment_item()
            if self.accept_kw('КАК'):
                self.next()
            self.accept_op(',')

    def parse_fragment_item(self):
        # поле с .* / .( … ) / КАК алиасом либо произвольное выражение-условие
        if self.peek()[0] == 'id':
            save = self.i
            parts = [self.next()[1]]
            star = False
            while self.accept_op('.'):
                if self.accept_op('*'):
                    star = True
                    break
                if self.peek()[0] == 'op' and self.peek()[1] == '(':
                    self.next()
                    self._parse_call_args()
                    break
                parts.append(self.next()[1])
            if not star and self.accept_op('('):
                self._parse_call_args()  # inline-развертка .( … )
            if self.accept_kw('КАК'):
                self.next()
                return
            if star:
                return
            self.i = save
        self._parse_expr_with_star()

    def parse_select_item(self):
        if self.accept_op('*'):
            return ('star',)
        expr = self.parse_expr()
        return ('expr', expr, self._parse_alias_tail())

    def parse_expr_list(self):
        exprs = [self.parse_expr()]
        while self.accept_op(','):
            exprs.append(self.parse_expr())
        return exprs

    def parse_order(self):
        order = []
        while True:
            expr = self.parse_expr()
            direction = 'asc'
            if self.accept_kw('ИЕРАРХИЯ'):
                # упорядочивание по иерархии: ВОЗР неявно, УБЫВ опционально
                if self.accept_kw('УБЫВ'):
                    direction = 'desc'
            elif self.accept_kw('ВОЗР'):
                direction = 'asc'
            elif self.accept_kw('УБЫВ'):
                direction = 'desc'
            order.append((expr, direction))
            if not self.accept_op(','):
                return order

    def parse_totals(self):
        # ИТОГИ [<итоговые поля>] ПО [ОБЩИЕ] <контрольные точки>;
        # форма «ИТОГИ ОБЩИЕ» допускает отсутствие ПО
        aggregates = None
        if not (self.is_kw('ПО') or self.is_kw('ОБЩИЕ')):
            aggregates = [self.parse_total_point()]
            while self.accept_op(','):
                aggregates.append(self.parse_total_point())
        self.accept_kw('ПО')
        self.accept_kw('ОБЩИЕ')
        fields = None
        if self.peek()[0] not in ('eof',) and not self._at_clause_boundary():
            fields = [self.parse_total_point()]
            while self.accept_op(','):
                fields.append(self.parse_total_point())
        return {'aggregates': aggregates, 'fields': fields}

    def _at_clause_boundary(self):
        """Следующий токен начинает новое предложение/секцию или конец."""
        tok = self.peek()
        if tok[0] == 'op' and tok[1] in (';', '}'):
            return True
        return tok[0] == 'id' and tok[1].upper() in (
            'ИЗ', 'ГДЕ', 'СГРУППИРОВАТЬ', 'ИМЕЮЩИЕ', 'ИТОГИ', 'ПОРЯДОК',
            'УПОРЯДОЧИТЬ', 'ИНДЕКСИРОВАТЬ', 'АВТОУПОРЯДОЧИВАНИЕ', 'ДЛЯ',
            'ОБЪЕДИНИТЬ', 'УНИЧТОЖИТЬ', 'ВЫБРАТЬ')

    def parse_total_point(self):
        """<Выражение> [[ТОЛЬКО] ИЕРАРХИЯ | ПЕРИОДАМИ(…)] [[КАК] псевдоним]."""
        expr = self.parse_expr()
        if self.accept_kw('ТОЛЬКО'):
            self.expect_kw('ИЕРАРХИЯ')
        elif self.accept_kw('ИЕРАРХИЯ'):
            pass
        elif self.accept_kw('ПЕРИОДАМИ'):
            self.expect_op('(')
            self.expect_kw('СЕКУНДА', 'МИНУТА', 'ЧАС', 'ДЕНЬ', 'НЕДЕЛЯ',
                           'МЕСЯЦ', 'КВАРТАЛ', 'ГОД', 'ДЕКАДА', 'ПОЛУГОДИЕ')
            while self.accept_op(','):
                self.parse_expr()
            self.expect_op(')')
        self._parse_alias_tail()
        return expr

    def _parse_alias_tail(self):
        """Псевдоним: КАК обязателен лишь явно; платформа допускает [КАК]."""
        if self.accept_kw('КАК'):
            return self.next()[1]
        tok = self.peek()
        if tok[0] == 'id' and tok[1].upper() not in _KW:
            return self.next()[1]
        return None

    # -- источники ---------------------------------------------------------
    def parse_sources(self):
        base = self.parse_source()
        return {'base': base, 'joins': self._join_chain()}

    def _join_chain(self):
        """Соединения, включая вложенные (соединение при соединении без ПО)."""
        joins = []
        while True:
            self.parse_braced()  # области СКД между соединениями
            join_kind = None
            tok = self.peek()
            if tok[0] == 'id' and tok[1].upper() in _JOINS:
                self.next()
                join_kind = _JOINS[tok[1].upper()]
                self.accept_kw('ВНЕШНЕЕ')
                self.expect_kw('СОЕДИНЕНИЕ')
            elif tok[0] == 'id' and tok[1].upper() == 'СОЕДИНЕНИЕ':
                self.next()
                join_kind = 'inner'
            elif tok[0] == 'op' and tok[1] == ',':
                self.next()
                join_kind = 'cross'
            else:
                return joins
            source = self.parse_source()
            nested = self._join_chain()
            on_expr = None
            if join_kind != 'cross':
                self.expect_kw('ПО')
                on_expr = self.parse_expr()
            joins.extend(nested)
            joins.append((join_kind, source, on_expr))

    def _try_join(self):
        """Соединение внутри необязательной области {…}; True — если было."""
        tok = self.peek()
        if tok[0] == 'id' and tok[1].upper() in _JOINS:
            self.next()
            self.accept_kw('ВНЕШНЕЕ')
            self.expect_kw('СОЕДИНЕНИЕ')
        elif tok[0] == 'id' and tok[1].upper() == 'СОЕДИНЕНИЕ':
            self.next()
        else:
            return False
        self.parse_source()
        self.expect_kw('ПО')
        self.parse_expr()
        return True

    def parse_source(self):
        tok = self.peek()
        if tok[0] not in ('id', 'param') and not (tok[0] == 'op' and tok[1] == '('):
            raise self.error('Ожидался источник данных: таблица, параметр или запрос')
        if tok[0] == 'param':
            self.next()
            return ('param', tok[1], self._parse_alias())
        if tok[0] == 'op' and tok[1] == '(':
            self.next()
            sub = self.parse_select()
            self.expect_op(')')
            return ('query', sub, self._parse_alias())
        segments = [self.next()[1]]
        while self.accept_op('.'):
            segments.append(self.next()[1])
        args = None
        if self.accept_op('('):
            args = self._parse_call_args()
        return ('table', segments, args, self._parse_alias())

    def _parse_call_args(self):
        """Аргументы вызова; допускает пустые позиции, РАЗЛИЧНЫЕ, области {…},
        (Поле).* и КАК алиасы."""
        args = []
        while True:
            if self.accept_op(')'):
                return args
            if self.accept_op(','):
                args.append(None)
                continue
            if self.accept_op('{'):
                self._braced_block()
                args.append(None)
                continue
            if self.accept_op('*'):
                args.append(None)  # КОЛИЧЕСТВО(*)
                continue
            self.accept_kw('РАЗЛИЧНЫЕ')
            args.append(self._parse_expr_with_star())
            if self.accept_kw('КАК'):
                self.next()
            while self.accept_op('{'):
                self._braced_block()
            if not self.accept_op(','):
                self.expect_op(')')
                return args

    def _parse_expr_with_star(self):
        expr = self.parse_expr()
        if self.accept_op('.'):
            self.expect_op('*')
            return ('starof', expr)
        return expr

    def _parse_alias(self):
        return self._parse_alias_tail()

    # -- выражения ---------------------------------------------------------
    def parse_expr(self):
        return self.parse_or()

    def parse_or(self):
        left = self.parse_and()
        while True:
            self.parse_braced()  # области СКД между частями условия
            if self.is_kw('ИЛИ'):
                self.next()
                left = ('op', 'ИЛИ', left, self.parse_and())
            else:
                return left

    def parse_and(self):
        left = self.parse_not()
        while True:
            self.parse_braced()
            if self.is_kw('И'):
                self.next()
                left = ('op', 'И', left, self.parse_not())
            else:
                return left

    def parse_not(self):
        if self.accept_kw('НЕ'):
            return ('not', self.parse_not())
        return self.parse_cmp()

    def parse_cmp(self):
        left = self.parse_add()
        while True:
            tok = self.peek()
            if tok[0] == 'op' and tok[1] in ('=', '<', '>', '<=', '>=', '<>'):
                self.next()
                left = ('op', tok[1], left, self.parse_add())
                continue
            # НЕ перед В/МЕЖДУ/ПОДОБНО — отрицание оператора принадлежности
            neg = False
            if tok[0] == 'id' and tok[1].upper() == 'НЕ':
                nxt = self.peek(1)
                if nxt[0] == 'id' and nxt[1].upper() in ('В', 'МЕЖДУ', 'ПОДОБНО'):
                    self.next()
                    neg = True
                    tok = self.peek()
            if tok[0] == 'id' and tok[1].upper() == 'В':
                self.next()
                self.accept_kw('ИЕРАРХИИ')  # В ИЕРАРХИИ (…/запрос/&параметр)
                self.expect_op('(')
                if self.is_kw('ВЫБРАТЬ'):
                    sub = self.parse_select()
                    self.expect_op(')')
                    node = ('in', left, ('query', sub))
                else:
                    items = self.parse_expr_list()
                    self.expect_op(')')
                    node = ('in', left, ('list', items))
                left = ('not', node) if neg else node
                continue
            if tok[0] == 'id' and tok[1].upper() == 'ПОДОБНО':
                self.next()
                pattern = self.parse_add()
                spec = None
                if self.accept_kw('СПЕЦСИМВОЛ'):
                    spec = self.parse_add()  # спецсимвол экранирования
                node = ('like', left, pattern, spec)
                left = ('not', node) if neg else node
                continue
            if tok[0] == 'id' and tok[1].upper() == 'ССЫЛКА':
                # X ССЫЛКА Документ.Х — сравнение ссылки с таблицей
                self.next()
                parts = [self.next()[1]]
                while self.accept_op('.'):
                    parts.append(self.next()[1])
                left = ('refop', left, parts)
                continue
            if tok[0] == 'id' and tok[1].upper() == 'МЕЖДУ':
                self.next()
                low = self.parse_add()
                self.expect_kw('И')
                high = self.parse_add()
                node = ('between', left, low, high)
                left = ('not', node) if neg else node
                continue
            if tok[0] == 'id' and tok[1].upper() == 'ЕСТЬ':
                self.next()
                neg = bool(self.accept_kw('НЕ'))
                if self.accept_kw('NULL'):
                    left = ('isnull', left, not neg)
                else:
                    raise self.error('Ожидалось NULL после ЕСТЬ')
                continue
            return left

    def parse_add(self):
        left = self.parse_mul()
        while self.peek()[0] == 'op' and self.peek()[1] in ('+', '-'):
            op = self.next()[1]
            left = ('op', op, left, self.parse_mul())
        return left

    def parse_mul(self):
        # бинарные операции языка запросов: + - * / (без остатка от деления)
        left = self.parse_unary()
        while self.peek()[0] == 'op' and self.peek()[1] in ('*', '/'):
            op = self.next()[1]
            left = ('op', op, left, self.parse_unary())
        return left

    def parse_unary(self):
        tok = self.peek()
        if tok[0] == 'op' and tok[1] in ('+', '-'):
            self.next()
            return ('un', tok[1], self.parse_unary())
        return self.parse_primary()

    def parse_primary(self):
        tok = self.peek()
        if tok[0] == 'num':
            self.next()
            return ('num', tok[1])
        if tok[0] == 'str':
            self.next()
            return ('str', tok[1])
        if tok[0] == 'param':
            self.next()
            return ('param', tok[1])
        if tok[0] == 'op' and tok[1] == '{':
            # необязательная область СКД вокруг выражения: {(&П)} и т.п.
            self.next()
            expr = self.parse_expr()
            if self.accept_kw('КАК'):
                self.next()
            self.expect_op('}')
            return expr
        if tok[0] == 'op' and tok[1] == '(':
            self.next()
            if self.is_kw('ВЫБРАТЬ'):
                sub = self.parse_select()
                self.expect_op(')')
                return ('subquery', sub)
            expr = self.parse_expr()
            if self.accept_op(','):
                items = [expr] + self.parse_expr_list()
                self.expect_op(')')
                return ('tuple', items)
            self.expect_op(')')
            while self.accept_op('.'):
                if self.accept_op('*'):
                    return ('starof', expr)
                if self.peek()[0] == 'op' and self.peek()[1] == '(':
                    self.next()
                    self._parse_call_args()
                    break
                self.next()  # (X).Поле… — суффикс после скобок
            return expr
        if tok[0] == 'id':
            upper = tok[1].upper()
            if upper == 'NULL':
                self.next()
                return ('null',)
            if upper in ('ИСТИНА', 'ЛОЖЬ', 'НЕОПРЕДЕЛЕНО'):
                self.next()
                return ('bool', upper)
            if upper == 'ВЫБОР':
                return self.parse_case()
            if upper == 'ВЫРАЗИТЬ':
                self.next()
                self.expect_op('(')
                expr = self.parse_expr()
                self.expect_kw('КАК')
                type_str, type_parts = self.parse_type()
                self.expect_op(')')
                rest = []
                while self.accept_op('.'):
                    rest.append(self.next()[1])
                if rest:
                    return ('castref', expr, type_parts, rest)
                return ('cast', expr, type_str)
            self.next()
            if self.accept_op('('):
                return ('func', upper, self._parse_call_args())
            parts = [tok[1]]
            while self.accept_op('.'):
                if self.peek()[0] == 'op' and self.peek()[1] == '(':
                    self.next()
                    self._parse_call_args()  # Т.Поле.(А КАК Б, …)
                    break
                parts.append(self.next()[1])
            return ('field', parts)
        raise self.error(f'Неожиданный токен {tok[1]!r}')

    def parse_case(self):
        self.expect_kw('ВЫБОР')
        if not self.is_kw('КОГДА'):
            self.parse_expr()  # операнд выбора
        branches = []
        while self.accept_kw('КОГДА'):
            when = self.parse_expr()
            self.expect_kw('ТОГДА')
            then = self.parse_expr()
            branches.append((when, then))
        else_e = None
        if self.accept_kw('ИНАЧЕ'):
            else_e = self.parse_expr()
        self.expect_kw('КОНЕЦ')
        return ('case', branches, else_e)

    def parse_type(self):
        parts = [self.next()[1]]
        while self.accept_op('.'):
            parts.append(self.next()[1])
        if self.accept_op('('):
            if not self.accept_op(')'):
                self.parse_expr_list()
                self.expect_op(')')
        return '.'.join(parts), parts


def parse_query(text):
    """Разбирает текст запроса; возвращает список операторов (словари)."""
    return _Parser(text).parse_script()


# ===========================================================================
# Семантический контроль по метаданным
# ===========================================================================

QUERY_PREFIXES = {
    'Справочник': 'Catalog',
    'Документ': 'Document',
    'Перечисление': 'Enum',
    'РегистрСведений': 'InformationRegister',
    'РегистрНакопления': 'AccumulationRegister',
    'РегистрРасчета': 'CalculationRegister',
    'РегистрБухгалтерии': 'AccountingRegister',
    'ПланВидовХарактеристик': 'ChartOfCharacteristicType',
    'ПланСчетов': 'ChartOfAccounts',
    'ПланВидовРасчета': 'ChartOfCalculationTypes',
    'БизнесПроцесс': 'BusinessProcess',
    'Задача': 'Task',
    'ОбменДанными': 'ExchangePlan',
    'ЖурналДокументов': 'DocumentJournal',
    'КритерийОтбора': 'FilterCriterion',
    'ХранилищеНастроек': 'SettingsStorage',
    'Константа': 'Constant',
}

VIRTUAL_TABLES = {
    'СрезПервых', 'СрезПоследних', 'Остатки', 'Обороты', 'ОстаткиИОбороты',
    'ОборотыНаДату', 'ОстаткиИОборотыНаДату', 'Движения', 'ДвиженияССубконто',
    'Записи', 'Перерасчет', 'График', 'ЗначенияСубконто', 'Последовательности',
    'Приходы', 'Расходы', 'ПустаяСсылка',
}

STANDARD_FIELDS = {
    'Ссылка', 'Наименование', 'Код', 'Владелец', 'Родитель', 'ПометкаУдаления',
    'Предопределенный', 'ИмяПредопределенныхДанных', 'Дата', 'Номер',
    'МоментВремени', 'Период', 'Регистратор', 'Активность', 'КлючЗаписи',
    'ВидДвижения', 'НомерСтроки', 'Тип', 'Значение', 'График', 'Порядок',
    'Счет', 'Сумма', 'Субконто1', 'Субконто2', 'Субконто3', 'Организация',
    'ЭтоГруппа', 'Проведен', 'ВерсияПотока', 'Использование',
    'Выполнена', 'БизнесПроцесс', 'ТочкаМаршрута',
    'ТочкаМаршрутаБизнесПроцесса', 'Исполнитель', 'Наименование',
    'Завершен', 'Стартован', 'ДатаЗавершения',
}

STANDARD_FIELDS_LOW = {f.lower() for f in STANDARD_FIELDS}


class MetaContext:
    """Справочник таблиц/полей/ссылок, построенный по базе метаданных."""

    def __init__(self, conn):
        q = conn.execute
        self.by_type_name = {}
        for path, typ, name in q('SELECT path, type, name FROM meta_object'):
            if path.count('/') == 1:
                for ru, stem in QUERY_PREFIXES.items():
                    if stem == typ:
                        self.by_type_name[f'{ru}.{name}'] = path
        self._fields = {}
        for path, name in q('SELECT o.path, a.name FROM meta_attribute a '
                            'JOIN meta_object o ON o.id=a.object_id'):
            self._fields.setdefault(path, set()).add(name.lower())
        # значения перечислений и предопределённые элементы — поля объекта
        self._enum = {}
        for path, name in q('SELECT o.path, e.name FROM enum_value e '
                            'JOIN meta_object o ON o.id=e.object_id'):
            self._enum.setdefault(path, set()).add(name.lower())
        self._predef = {}
        for path, name in q('SELECT o.path, p.name FROM predefined p '
                            'JOIN meta_object o ON o.id=p.object_id'):
            self._predef.setdefault(path, set()).add(name.lower())
        # табличные части: имя секции и её поля
        self._sections = {}
        self._section_names = {}
        for path, sec, name in q(
                'SELECT o.path, a.tabular, a.name FROM meta_attribute a '
                'JOIN meta_object o ON o.id=a.object_id WHERE a.tabular IS NOT NULL'):
            self._sections.setdefault(path, {}).setdefault(
                sec.lower(), set()).add(name.lower())
            self._section_names.setdefault(path, set()).add(sec.lower())
        # общие реквизиты: поля объектов из привязки (без привязки — всех)
        self._common = {}
        for name, target in q(
                'SELECT c.name, t.path FROM meta_object c '
                'LEFT JOIN common_target ct ON ct.common_id=c.id '
                'LEFT JOIN meta_object t ON t.id=ct.target_id '
                "WHERE c.type='CommonAttribute'"):
            low = name.lower()
            if low not in self._common:
                self._common[low] = None if target is None else set()
            if self._common[low] is not None and target is not None:
                self._common[low].add(target)
        # объекты, чьи реквизиты не извлеклись из заголовка: поля не проверяем
        self._open_paths = ({p for (p,) in q('SELECT path FROM meta_object')}
                            - set(self._fields))
        self._refs = {}
        for path, name, target in q(
                'SELECT v.path, a.name, t.path FROM attribute_ref r '
                'JOIN meta_attribute a ON a.id=r.attribute_id '
                'JOIN meta_object v ON v.id=a.object_id '
                'JOIN meta_object t ON t.id=r.object_id'):
            self._refs.setdefault((path, name.lower()), []).append(target)

    def resolve_table(self, segments):
        """Путь объекта по первым двум сегментам таблицы запроса или None."""
        if len(segments) < 2:
            return None
        return self.by_type_name.get(f'{segments[0]}.{segments[1]}')

    def table_ok(self, segments):
        return self.resolve_table(segments) is not None

    def is_open(self, path):
        return path in self._open_paths

    def has_field(self, path, name):
        low = name.lower()
        if low in STANDARD_FIELDS_LOW:
            return True
        if low in self._fields.get(path, ()):
            return True
        if low in self._enum.get(path, ()):
            return True
        if low in self._predef.get(path, ()):
            return True
        if low in self._section_names.get(path, ()):
            return True
        targets = self._common.get(low)
        return targets is not None and (not targets or path in targets)

    def field_targets(self, path, name):
        return self._refs.get((path, name.lower()), [])


SYSTEM_PREFIXES = {
    'ВидДвиженияНакопления', 'ВидДвиженияБухгалтерии', 'ВидСубконто',
    'ВидРасчета', 'РежимЗапуска', 'СтатусСообщения', 'ТипВнешнейКомпоненты',
}


def check_query(text, ctx):
    """Проверяет запрос; возвращает список сообщений об ошибках (пустой = ок)."""
    errors = []
    try:
        statements = parse_query(text)
    except QueryError as err:
        return [f'синтаксис: {err}']
    # первый проход: собираем временные таблицы ПОМЕСТИТЬ по всему скрипту
    temp = {}
    for st in statements:
        if 'destroy' not in st:
            _collect_puts(st, temp)
    for st in statements:
        if 'destroy' not in st:
            _check_select(st, ctx, temp, errors)
    return errors


def _collect_puts(node, temp):
    if node.get('put') and node['put'] not in temp:
        temp[node['put']] = _select_columns(node)
    for _, sub in node.get('union', []):
        _collect_puts(sub, temp)


def _select_columns(node):
    cols = set()
    for item in node['items']:
        if item[0] == 'star':
            return None
        if item[2]:
            cols.add(item[2])
        elif item[1][0] == 'field':
            cols.add(item[1][1][-1])
    return cols


def _check_select(node, ctx, temp, errors):
    scope = {}
    if node['source']:
        _register_source(node['source']['base'], ctx, temp, scope, errors)
        for _, source, on_expr in node['source']['joins']:
            _register_source(source, ctx, temp, scope, errors)
            _check_expr(on_expr, ctx, scope, errors)
    if node['put']:
        temp[node['put']] = _select_columns(node)
    for item in node['items']:
        if item[0] == 'expr':
            _check_expr(item[1], ctx, scope, errors)
    for key in ('where', 'having'):
        if node[key]:
            _check_expr(node[key], ctx, scope, errors)
    for expr in (node['group'] or []):
        _check_expr(expr, ctx, scope, errors)
    for expr, _ in (node['order'] or []):
        _check_expr(expr, ctx, scope, errors)
    if node['totals']:
        for expr in (node['totals']['aggregates'] or []) + \
                (node['totals']['fields'] or []):
            _check_expr(expr, ctx, scope, errors)
    for _, sub in node['union']:
        _check_select(sub, ctx, temp, errors)


def _register_source(source, ctx, temp, scope, errors):
    kind = source[0]
    alias = source[-1]
    if kind == 'table':
        segments = source[1]
        name0 = segments[0]
        if name0 in temp:
            info = ('derived', temp[name0])
        elif len(segments) == 1:
            # односегментная таблица вне temp — временная таблица из другого
            # набора данных той же СКД: не ошибка, поля не проверяем
            info = ('open', None)
        elif not ctx.table_ok(segments):
            errors.append(f'неизвестная таблица: {".".join(segments)}')
            info = ('open', None)
        elif any(seg in VIRTUAL_TABLES for seg in segments[2:]) or \
                ctx.is_open(ctx.resolve_table(segments)):
            # виртуальные таблицы (производные поля) и объекты без реквизитов
            info = ('open', ctx.resolve_table(segments))
        else:
            info = ('obj', ctx.resolve_table(segments))
        name = alias or name0
    elif kind == 'param':
        info = ('open', None)
        name = alias or source[1]
    else:
        info = ('derived', _select_columns(source[1]))
        name = alias or ''
    if name:
        scope[name] = info


def _check_expr(expr, ctx, scope, errors):
    if not isinstance(expr, tuple):
        return
    kind = expr[0]
    if kind == 'field':
        _check_field(expr[1], ctx, scope, errors)
    elif kind == 'op':
        _check_expr(expr[2], ctx, scope, errors)
        _check_expr(expr[3], ctx, scope, errors)
    elif kind in ('not', 'un'):
        _check_expr(expr[1], ctx, scope, errors)
    elif kind == 'func':
        for arg in expr[2]:
            if arg is not None:
                _check_expr(arg, ctx, scope, errors)
    elif kind == 'refop':
        _check_expr(expr[1], ctx, scope, errors)
        if not ctx.table_ok(expr[2]):
            errors.append(f'неизвестная таблица: {".".join(expr[2][:2])}')
    elif kind == 'cast':
        _check_expr(expr[1], ctx, scope, errors)
    elif kind == 'case':
        for when, then in expr[1]:
            _check_expr(when, ctx, scope, errors)
            _check_expr(then, ctx, scope, errors)
        if expr[2]:
            _check_expr(expr[2], ctx, scope, errors)
    elif kind == 'in':
        _check_expr(expr[1], ctx, scope, errors)
        if expr[2][0] == 'list':
            for sub in expr[2][1]:
                _check_expr(sub, ctx, scope, errors)
        elif expr[2][0] == 'query':
            _check_select(expr[2][1], ctx, {}, errors)
    elif kind == 'between':
        for sub in expr[1:]:
            _check_expr(sub, ctx, scope, errors)
    elif kind == 'tuple':
        for sub in expr[1]:
            _check_expr(sub, ctx, scope, errors)
    elif kind == 'subquery':
        _check_select(expr[1], ctx, {}, errors)
    elif kind == 'starof':
        _check_expr(expr[1], ctx, scope, errors)
    elif kind == 'castref':
        _check_castref(expr, ctx, errors)
    elif kind == 'like':
        _check_expr(expr[1], ctx, scope, errors)
        _check_expr(expr[2], ctx, scope, errors)  # шаблон может быть выражением
    elif kind == 'isnull':
        _check_expr(expr[1], ctx, scope, errors)


def _check_castref(expr, ctx, errors):
    """ВЫРАЗИТЬ(X КАК Тип).Поле — поле объекта-типа."""
    _, _, type_parts, rest = expr
    if type_parts and type_parts[0] in QUERY_PREFIXES:
        if not ctx.table_ok(type_parts):
            return  # приведение к абстрактному типу (ДокументОснование и т.п.)
        path = ctx.resolve_table(type_parts)
        if ctx.is_open(path):
            return
        for name in rest:
            if not ctx.has_field(path, name):
                errors.append(f'неизвестное поле: {path} :: {name}')
                return


def _check_field(parts, ctx, scope, errors):
    head = parts[0]
    if head in QUERY_PREFIXES:
        # аргумент вида ЗНАЧЕНИЕ(Справочник.Х.ПустаяСсылка); приведения к
        # абстрактным типам (Документ.ДокументОснование) не проверяем
        return
    if head in SYSTEM_PREFIXES:
        return  # системные перечисления (ВидДвиженияНакопления.Приход и т.п.)
    if head in scope:
        info = scope[head]
    elif len(scope) == 1:
        info = next(iter(scope.values()))
    else:
        return  # поле без алиаса при нескольких источниках — не проверяем
    rest = parts[1:]
    if info[0] == 'derived':
        # временные/вложенные таблицы могут строиться в других наборах СКД
        return
    if info[0] != 'obj' or info[1] is None:
        return
    path = info[1]
    section = None
    for i, name in enumerate(rest):
        if section is not None:
            # поле табличной части
            fields = ctx._sections.get(path, {}).get(section)
            if not fields or name.lower() not in fields:
                return  # мягко: состав секции мог извлечься не полностью
        elif not ctx.has_field(path, name):
            # первое поле источника проверяем строго; глубже — мягко:
            # цепочки разыменования могут проходить через табличные части
            if i == 0:
                errors.append(f'неизвестное поле: {path} :: {name}')
            return
        if i + 1 < len(rest):
            if section is None and name.lower() in ctx._section_names.get(path, ()):
                section = name.lower()  # следующий шаг — внутрь табличной части
            else:
                targets = ctx.field_targets(path, name)
                if len(targets) == 1:
                    path = targets[0]
                    section = None
                else:
                    return  # абстрактный/составной тип — глубже не проверяем
