"""Маленький структурный валидатор для чекеров schema: без внешних зависимостей.

Спецификация поля — один из вариантов:
  str, int, bool, float, "number"     — тип значения (bool не считается int/number);
  "any"                                — любое значение;
  [spec]                               — список элементов spec;
  {name: spec}                         — объект с обязательными полями;
  Optional(spec)                       — поле объекта может отсутствовать или быть null;
  NonEmpty(spec)                       — строка или список не пустые;
  OneOf(*values)                       — одно из значений.
"""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Optional:
    spec: Any


@dataclass(frozen=True)
class NonEmpty:
    spec: Any


class OneOf:
    def __init__(self, *values: Any):
        self.values = values


def errors(value: Any, spec: Any, path: str = "$") -> list[str]:
    if isinstance(spec, Optional):
        return [] if value is None else errors(value, spec.spec, path)
    if isinstance(spec, NonEmpty):
        found = errors(value, spec.spec, path)
        if not found and not value:
            found.append(f"{path}: must not be empty")
        return found
    if isinstance(spec, OneOf):
        return [] if value in spec.values else [f"{path}: must be one of {list(spec.values)}"]
    if spec == "any":
        return []
    if spec == "number":
        ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        return [] if ok else [f"{path}: must be a number"]
    if spec in (str, int, bool, float):
        ok = isinstance(value, spec) and not (spec is int and isinstance(value, bool))
        return [] if ok else [f"{path}: must be {spec.__name__}"]
    if isinstance(spec, list):
        if not isinstance(value, list):
            return [f"{path}: must be a list"]
        return [e for i, item in enumerate(value) for e in errors(item, spec[0], f"{path}[{i}]")]
    if isinstance(spec, dict):
        if not isinstance(value, dict):
            return [f"{path}: must be an object"]
        found = []
        for name, field_spec in spec.items():
            if name not in value:
                if not isinstance(field_spec, Optional):
                    found.append(f"{path}.{name}: is required")
                continue
            found.extend(errors(value[name], field_spec, f"{path}.{name}"))
        return found
    raise TypeError(f"unsupported spec at {path}: {spec!r}")
