"""后置断言：从例子里挖出来的结构性质，运行时当警报器用。

两份文档在这里打架，取舍写在下面：

- [design.md §8.2](../../docs/design.md#82-guard) 说后置断言"从需求和例子里自动
  提炼"，失败动作是 `postcondition_failed` —— 也就是**阻断**。
- [correctness.md §4.2](../../docs/correctness.md#42-性质从哪来) 说未经调用方确认
  的性质**只做警告，绝不阻断**：拿一条自己猜出来的性质去否决一个正确结果，
  比漏个 bug 难查得多。

后者赢。这里挖出来的性质没人确认过，所以违反只记一笔警告、把输入存进测试集，
不拦结果。等 M1 的确认流做出来，确认过的性质才有资格阻断（那时函数也就够得上
`CONFIRMED` 了）。**返回 schema 不在此列** —— 它不是猜的，是从例子结构直接读出来
的，而且在 200 个模糊输入上验过，所以它照旧阻断。

安装门槛：一条性质要在**全部适用的例子**上成立，且至少适用于 2 个例子。只在
1 个例子上成立的"性质"是巧合。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

# 一条性质对某个 (输入, 输出) 的判定：True 成立 / False 违反 / None 不适用。
Verdict = bool | None


def _only_list(value: Any) -> list | None:
    """输入里唯一的数组。有零个或多个就返回 None —— 说不清是哪个，就别猜。"""
    if not isinstance(value, dict):
        return value if isinstance(value, list) else None
    lists = [v for v in value.values() if isinstance(v, list)]
    return lists[0] if len(lists) == 1 else None


def _strings_in(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        return set().union(*(_strings_in(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(_strings_in(v) for v in value)) if value else set()
    return set()


def _size_preserved(inp: Any, out: Any) -> Verdict:
    """输入的数组多长，输出就多长。排序、打标、映射类的函数都满足它。"""
    src = _only_list(inp)
    if src is None or not isinstance(out, list):
        return None
    return len(out) == len(src)


def _keys_from_input(inp: Any, out: Any) -> Verdict:
    """输出对象的键，都是输入里出现过的字符串。分组聚合类函数满足它 ——
    凭空长出一个键，几乎总是清洗逻辑写错了。"""
    if not isinstance(out, dict) or not out:
        return None
    return set(out.keys()) <= _strings_in(inp)


def _empty_in_empty_out(inp: Any, out: Any) -> Verdict:
    """空进空出。只在输入确实有个数组、且输出是容器时适用。"""
    src = _only_list(inp)
    if src is None or not isinstance(out, (list, dict)):
        return None
    return bool(src) or not out


@dataclass(frozen=True)
class Property:
    name: str
    why: str
    holds: Callable[[Any, Any], Verdict]


CATALOG: tuple[Property, ...] = (
    Property("size_preserved", "输出长度应与输入数组长度一致", _size_preserved),
    Property("keys_from_input", "输出的键应该都在输入里出现过", _keys_from_input),
    Property("empty_in_empty_out", "输入为空时输出也该为空", _empty_in_empty_out),
)


def mine(examples) -> list[str]:
    """从例子里挖出成立的性质。返回性质名，存进版本 meta 供运行时复用。"""
    out = []
    for p in CATALOG:
        verdicts = [p.holds(e.input, e.output) for e in examples]
        applicable = [v for v in verdicts if v is not None]
        if len(applicable) >= 2 and all(applicable):
            out.append(p.name)
    return out


def check(names: list[str], inp: Any, out: Any) -> list[str]:
    """运行时核对。返回被违反的性质说明；空列表 = 没话说。"""
    by_name = {p.name: p for p in CATALOG}
    broken = []
    for n in names:
        p = by_name.get(n)
        if p is None:
            continue                       # 性质库改过了，老函数的旧名字直接忽略
        if p.holds(inp, out) is False:
            broken.append(f"{p.name}：{p.why}")
    return broken
