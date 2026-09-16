"""Hold-out 分割。见 docs/correctness.md §9。

修复循环会把失败用例反馈给模型。跑几轮之后模型完全可能写出一份只对可见用例
正确的代码 —— 极端情况是 `if input == X: return Y`。所以留一部分用例出来，
修复循环完全看不到，最后验收时才跑。
"""
from __future__ import annotations

import math
import random

from .types import Example


class NotEnoughExamples(ValueError):
    pass


def split(
    examples: list[Example], ratio: float = 0.3, seed: int = 0, rotation: int = 0
) -> tuple[list[Example], list[Example]]:
    """确定性分割。rotation 用于 hold-out 挂掉后换一组重试（只允许一次）。"""
    n = len(examples)
    if n < 2:
        raise NotEnoughExamples(f"至少需要 2 个用例才能分出 hold-out，实际 {n}")

    idx = list(range(n))
    random.Random(seed + rotation * 9973).shuffle(idx)
    k = min(max(1, math.ceil(n * ratio)), n - 1)      # 两边都至少留 1 个
    held = set(idx[:k])
    return (
        [e for i, e in enumerate(examples) if i not in held],
        [e for i, e in enumerate(examples) if i in held],
    )
