"""离线测试替身：模拟 SQLAlchemy 2.0 声明式模型与会话的最小子集。

仅供 tests/ 集成测试通过 sys.path 优先加载，不参与应用正常运行。
模型统一以整型 ``id`` 为主键（本项目所有表均如此）。
"""

_TABLE_REGISTRY: dict[str, type] = {}
_STORE: dict[str, dict[int, object]] = {}


class _Column:
    def __init__(self, name=None):
        self.name = name

    def __set_name__(self, owner, name):
        self.name = name

    def __get__(self, instance, owner):
        if instance is None:
            return _ColumnRef(self.name)
        return instance.__dict__.get(self.name)

    def __set__(self, instance, value):
        instance.__dict__[self.name] = value


def mapped_column(*args, **kwargs):
    return _Column()


class DeclarativeBase:
    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        table = cls.__dict__.get("__tablename__")
        if table:
            _TABLE_REGISTRY[table] = cls
            _STORE.setdefault(table, {})


class _ColumnRef:
    def __init__(self, name, descending=False):
        self.name = name
        self.descending = descending

    def desc(self):
        return _ColumnRef(self.name, True)

    def asc(self):
        return _ColumnRef(self.name, False)

    def isnot(self, value):
        return _Condition(lambda row, n=self.name, v=value: getattr(row, n) is not v)

    def is_(self, value):
        return _Condition(lambda row, n=self.name, v=value: getattr(row, n) is v)

    def __eq__(self, value):
        return _Condition(lambda row, n=self.name, v=value: getattr(row, n) == v)

    def __ne__(self, value):
        return _Condition(lambda row, n=self.name, v=value: getattr(row, n) != v)

    def __hash__(self):
        return hash(self.name)


class _Condition:
    def __init__(self, fn):
        self.fn = fn

    def matches(self, row):
        return self.fn(row)


class _Query:
    def __init__(self, model_cls, conditions=(), order=(), row_limit=None):
        self.model_cls = model_cls
        self.conditions = list(conditions)
        self.order = list(order)
        self.row_limit = row_limit

    def where(self, *conditions):
        return _Query(self.model_cls, [*self.conditions, *conditions], self.order, self.row_limit)

    def order_by(self, *columns):
        return _Query(self.model_cls, self.conditions, list(columns), self.row_limit)

    def limit(self, value):
        return _Query(self.model_cls, self.conditions, self.order, value)

    def with_for_update(self, **_):
        return self

    def evaluate(self):
        rows = list(_STORE[self.model_cls.__tablename__].values())
        for condition in self.conditions:
            rows = [row for row in rows if condition.matches(row)]
        for column in reversed(self.order):
            rows.sort(key=lambda row, c=column: (getattr(row, c.name) is None, getattr(row, c.name)),
                      reverse=column.descending)
        if self.row_limit is not None:
            rows = rows[: self.row_limit]
        return rows


def select(target):
    if isinstance(target, _CountExpr):
        return _CountQuery(target)
    return _Query(target)


class _CountExpr:
    def __init__(self):
        self.model_cls = None

    def select_from(self, model_cls):
        self.model_cls = model_cls
        return self


class _CountQuery(_Query):
    def __init__(self, expr):
        super().__init__(expr.model_cls)
        self.expr = expr

    def select_from(self, model_cls):
        self.model_cls = model_cls
        return self

    def evaluate(self):
        rows = _Query.evaluate(self)
        return [len(rows)]


class _Update:
    def __init__(self, model_cls):
        self.model_cls = model_cls
        self.conditions = []
        self.mapping = {}

    def where(self, *conditions):
        self.conditions = [*self.conditions, *conditions]
        return self

    def values(self, **mapping):
        self.mapping = mapping
        return self

    def apply(self):
        count = 0
        for row in _STORE[self.model_cls.__tablename__].values():
            if all(condition.matches(row) for condition in self.conditions):
                for key, value in self.mapping.items():
                    setattr(row, key, value)
                count += 1
        return count


def update(model_cls):
    return _Update(model_cls)


def ForeignKey(_target):
    return _Column()


def create_engine(*args, **kwargs):
    class _Engine:
        @staticmethod
        def dispose():
            pass

    return _Engine()


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _Session:
    def begin(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False

    def add(self, model):
        table = type(model).__tablename__
        if getattr(model, "id", None) is None:
            model.id = (max(_STORE[table].keys(), default=0) + 1)
        _STORE[table][model.id] = model

    def flush(self):
        pass

    def commit(self):
        pass

    def rollback(self):
        pass

    def get(self, model_cls, target_id):
        return _STORE[model_cls.__tablename__].get(target_id)

    def scalars(self, query):
        if isinstance(query, _Update):
            return _Result([query.apply()])
        return _Result(query.evaluate())

    def scalar(self, query):
        rows = query.evaluate()
        return rows[0] if rows else None

    def execute(self, statement):
        if isinstance(statement, _Update):
            return _Result([statement.apply()])
        return _Result([])


def sessionmaker(bind=None, **_):
    def factory():
        return _Session()

    # 真实 SQLAlchemy 的 sessionmaker 实例本身也代理 Session 的上下文方法。
    factory.begin = lambda: _Session().begin()
    return factory


def reset_store():
    for table in _STORE.values():
        table.clear()


# 模型字段类型在替身里不需要实际行为。
class _NullType:
    def __call__(self, *args, **kwargs):
        return self

    def __getattr__(self, _):
        return self


String = Text = JSON = _NullType()


class _Func:
    def __getattr__(self, name):
        if name == "count":
            return lambda: _CountExpr()
        return _ColumnRef(name)


func = _Func()


def text(sql):
    return sql
