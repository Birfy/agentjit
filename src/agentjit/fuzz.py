"""T5 —— 属性/模糊测试。见 docs/correctness.md §7。

只检查不需要语义知识的东西：不崩、不超时、返回值符合 schema、确定性。
抓不到语义错误，但能抓住一大批健壮性问题 —— 生成代码最常见的毛病恰恰是
"happy path 写对了，遇到空值/异常格式就炸"。

输入来自两处：schema 随机生成，以及**从例子扰动出来的变体**。后者很重要 ——
纯 schema 生成出来的东西离真实数据可以很远，而例子是真实的。
"""
from __future__ import annotations

import copy
import random
from typing import Any

# 偏向边界的取值池。生成代码挂掉的地方几乎总在这些值上。
_STRINGS = ["", " ", "0", "-", "null", "None", "0.0", "1,234.56", "$1,200.50",
            "abc", "  spaced  ", "\n", "\t", "很长" * 200, "-50", "1e9", "NaN",
            "0x1f", "２３", "'\"\\", "%s", "{}"]
_NUMBERS = [0, 1, -1, 2, -2, 0.0, -0.0, 0.1, 1e9, -1e9, 1e-9, 2**31, -(2**31), 999999999999]
_INTS = [0, 1, -1, 2, -2, 10, 2**31 - 1, -(2**31), 999999999]


def generate(schema: dict[str, Any], rng: random.Random, depth: int = 0) -> Any:
    t = schema.get("type")
    if "enum" in schema:
        return rng.choice(schema["enum"])
    if isinstance(t, list):
        t = rng.choice(t)

    if t == "object" or (t is None and "properties" in schema):
        props = schema.get("properties", {})
        required = set(schema.get("required", props.keys()))
        out = {}
        for k, sub in props.items():
            # 可选字段按概率省略 —— 缺字段是最常见的崩溃来源
            if k in required or rng.random() < 0.75:
                out[k] = generate(sub, rng, depth + 1)
        return out
    if t == "array":
        item = schema.get("items", {"type": "string"})
        n = rng.choice([0, 0, 1, 1, 2, 3, 5, 12] if depth < 2 else [0, 1, 2])
        arr = [generate(item, rng, depth + 1) for _ in range(n)]
        if arr and rng.random() < 0.25:        # 重复元素：分组/去重逻辑的常见坑
            arr.append(copy.deepcopy(rng.choice(arr)))
        return arr
    if t == "string":
        return rng.choice(_STRINGS)
    if t == "integer":
        return rng.choice(_INTS)
    if t == "number":
        return rng.choice(_NUMBERS)
    if t == "boolean":
        return rng.choice([True, False])
    if t == "null":
        return None
    return rng.choice([None, 0, "", [], {}])


def perturb(value: Any, rng: random.Random) -> Any:
    """从一个真实例子扰动出变体。比纯随机生成更接近真实分布。"""
    v = copy.deepcopy(value)
    if isinstance(v, dict):
        if not v:
            return v
        k = rng.choice(list(v))
        op = rng.random()
        if op < 0.30:
            v.pop(k)                                   # 删字段
        elif op < 0.55:
            v[k] = rng.choice([None, "", 0, [], {}])   # 换成空值
        else:
            v[k] = perturb(v[k], rng)
    elif isinstance(v, list):
        if not v:
            return [rng.choice(_STRINGS)]
        op = rng.random()
        if op < 0.25:
            return []                                   # 清空
        if op < 0.45:
            return v + [copy.deepcopy(rng.choice(v))]   # 重复
        if op < 0.65:
            rng.shuffle(v)                              # 换序
        else:
            i = rng.randrange(len(v))
            v[i] = perturb(v[i], rng)
    elif isinstance(v, str):
        return rng.choice(_STRINGS)
    elif isinstance(v, bool):
        return not v
    elif isinstance(v, (int, float)):
        return rng.choice(_NUMBERS)
    return v


def make_inputs(
    param_schema: dict[str, Any],
    seed_examples: list[dict[str, Any]],
    n: int,
    seed: int = 0,
) -> list[dict[str, Any]]:
    rng = random.Random(seed)
    out: list[dict[str, Any]] = []
    for i in range(n):
        if seed_examples and i % 2 == 0:
            out.append(perturb(rng.choice(seed_examples), rng))
        else:
            got = generate(param_schema, rng)
            out.append(got if isinstance(got, dict) else {"value": got})
    return out
