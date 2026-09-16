"""从例子反推 schema。见 docs/design.md §6.2 —— 例子的第二个作用。

参数和返回结构直接从例子结构读出来，不用让模型猜，也不用调用方手写 schema。
"""
from __future__ import annotations

from typing import Any


def _leaf(values: list[Any]) -> dict[str, Any] | None:
    kinds = set()
    for v in values:
        if isinstance(v, bool):
            kinds.add("boolean")
        elif isinstance(v, int):
            kinds.add("integer")
        elif isinstance(v, float):
            kinds.add("number")
        elif isinstance(v, str):
            kinds.add("string")
        elif v is None:
            kinds.add("null")
        else:
            return None
    if {"integer", "number"} <= kinds:                # int 和 float 混用就是 number
        kinds = (kinds - {"integer"}) | {"number"}
    return {"type": kinds.pop() if len(kinds) == 1 else sorted(kinds)}


def infer(values: list[Any]) -> dict[str, Any]:
    values = [v for v in values]
    if not values:
        return {}
    if all(isinstance(v, dict) for v in values):
        return _object(values)
    if all(isinstance(v, list) for v in values):
        items = [x for v in values for x in v]
        return {"type": "array", "items": infer(items) if items else {}}
    return _leaf(values) or {}


def _object(dicts: list[dict]) -> dict[str, Any]:
    union: set[str] = set().union(*(d.keys() for d in dicts))
    common: set[str] = set.intersection(*(set(d.keys()) for d in dicts))

    # 分辨"记录"和"映射"：{type, amount} 每次都是这两个键 —— 记录；
    # {refund: .., sale: ..} 每次键都不同 —— 映射。
    # 把映射当记录推，会把 properties 钉死在见过的键上，一个新键就判不合法。
    map_like = len(dicts) >= 2 and (not common or len(union) > 2 * len(common))
    if map_like:
        vals = [v for d in dicts for v in d.values()]
        return {"type": "object", "additionalProperties": infer(vals) if vals else True}

    props = {k: infer([d[k] for d in dicts if k in d]) for k in sorted(union)}
    out: dict[str, Any] = {"type": "object", "properties": props}
    if len(dicts) >= 2:
        out["required"] = sorted(common)              # 单个例子推不出"必填"，别钉死
    return out


def spec_schemas(examples) -> tuple[dict, dict]:
    """从例子推出 (param_schema, return_schema)。"""
    return infer([e.input for e in examples]), infer([e.output for e in examples])
