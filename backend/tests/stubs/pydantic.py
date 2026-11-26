"""离线测试替身：pydantic 的最小子集，支持本项目用到的 BaseModel/Field/ConfigDict。"""

from typing import Annotated, get_args, get_origin


class ConfigDict(dict):
    pass


class _FieldInfo:
    def __init__(self, default=None, default_factory=None, **constraints):
        self.default = default
        self.default_factory = default_factory
        self.constraints = constraints


def Field(default=None, default_factory=None, **constraints):
    return _FieldInfo(default, default_factory, **constraints)


def constr(**kwargs):
    return str


class BaseModel:
    model_config = ConfigDict()

    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        cls._collect_fields()

    @classmethod
    def _collect_fields(cls):
        fields = {}
        for klass in reversed(cls.__mro__):
            annotations = getattr(klass, "__annotations__", {})
            for name, annotation in annotations.items():
                if name.startswith("_"):
                    continue
                # 只在该类自己的命名空间里找默认值，避免沿继承链取到无关对象。
                default = klass.__dict__.get(name, _UNSET)
                fields[name] = (annotation, default)
        cls._fields = fields

    def __init__(self, **kwargs):
        extra_forbid = self.model_config.get("extra") == "forbid"
        if extra_forbid:
            unknown = set(kwargs) - set(self._fields)
            if unknown:
                raise ValueError(f"多余字段: {sorted(unknown)}")
        for name, (annotation, default) in self._fields.items():
            if name in kwargs:
                value = kwargs[name]
            elif isinstance(default, _FieldInfo):
                if default.default_factory is not None:
                    value = default.default_factory()
                elif default.default is not _UNSET:
                    value = default.default
                else:
                    raise ValueError(f"缺少必填字段: {name}")
            elif default is not _UNSET:
                value = default
            else:
                raise ValueError(f"缺少必填字段: {name}")
            field_info = default if isinstance(default, _FieldInfo) else None
            value = self._validate(name, annotation, value, field_info)
            if self.model_config.get("str_strip_whitespace") and isinstance(value, str):
                value = value.strip()
            setattr(self, name, value)

    @staticmethod
    def _validate(name, annotation, value, field_info):
        origin = get_origin(annotation)
        if origin is Annotated:
            annotation = get_args(annotation)[0]
        # 嵌套模型列表（如 list[CoilEntry]）：逐元素实例化。
        if origin in (list, list):
            inner = get_args(annotation)[0]
            if not isinstance(value, list):
                raise ValueError(f"{name} 须为列表")
            if isinstance(inner, type) and issubclass(inner, BaseModel):
                return [item if isinstance(item, BaseModel) else inner(**item) for item in value]
            return value
        # 直接嵌套的 BaseModel。
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            return value if isinstance(value, BaseModel) else annotation(**value)
        if isinstance(annotation, type):
            if annotation in (int, float) and not isinstance(value, annotation):
                raise ValueError(f"{name} 类型不符")
            if annotation is str and not isinstance(value, str):
                raise ValueError(f"{name} 须为字符串")
        if field_info:
            constraints = field_info.constraints
            if "min_length" in constraints and hasattr(value, "__len__") and len(value) < constraints["min_length"]:
                raise ValueError(f"{name} 长度不足")
            if "max_length" in constraints and hasattr(value, "__len__") and len(value) > constraints["max_length"]:
                raise ValueError(f"{name} 长度超限")
            if "ge" in constraints and value < constraints["ge"]:
                raise ValueError(f"{name} 低于下限")
            if "le" in constraints and value > constraints["le"]:
                raise ValueError(f"{name} 高于上限")
            if "gt" in constraints and value <= constraints["gt"]:
                raise ValueError(f"{name} 须大于 {constraints['gt']}")
        return value

    def model_dump(self):
        return {name: getattr(self, name) for name in self._fields}


class _Unset:
    pass


_UNSET = _Unset()
