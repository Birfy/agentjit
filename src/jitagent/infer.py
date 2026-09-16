"""Infer the schema from the examples — design.md §6.2, the examples' second job.

The parameter and return shapes are read straight off the examples, so neither the
model has to guess them nor the caller has to hand-write a schema.
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
    if {"integer", "number"} <= kinds:                # mixing int and float means number
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

    # Tell a record from a map: {type, amount} has the same two keys every time — a
    # record; {refund: .., sale: ..} has different keys every time — a map. Inferring a
    # map as a record pins `properties` to the keys seen so far, and one new key then
    # makes a perfectly valid input illegal.
    map_like = len(dicts) >= 2 and (not common or len(union) > 2 * len(common))
    if map_like:
        vals = [v for d in dicts for v in d.values()]
        return {"type": "object", "additionalProperties": infer(vals) if vals else True}

    props = {k: infer([d[k] for d in dicts if k in d]) for k in sorted(union)}
    out: dict[str, Any] = {"type": "object", "properties": props}
    if len(dicts) >= 2:
        out["required"] = sorted(common)              # one example cannot establish "required"
    return out


def spec_schemas(examples) -> tuple[dict, dict]:
    """Derive (param_schema, return_schema) from the examples."""
    return infer([e.input for e in examples]), infer([e.output for e in examples])
