"""成本核算。[design.md §9](../../docs/design.md#9-指标) 里那个 `net_savings`。

整个系统押的是一句话：**合成一次 + 验证，比让 agent 自己做 N 遍便宜。** 这句话
要么能算出来，要么就是营销。所以这里把它拆成三个数，每个都标明出处：

    每次调用省下的 = agent 自己做一次的成本 − 走缓存函数一次的成本
    net_savings    = Σ 每次调用省下的 − 合成成本
    回本点          = 合成成本 / 每次调用省下的

**假设都摆在外面，不藏在公式里。** 其中只有 `reasoning_tokens` 是拍的 ——
它恰恰是节省的主要来源，所以它错了整个数就错了。等 NEXT.md 第 0 项（真实合成）
跑通、有了真实轨迹再标定。在那之前，打印出来的数要一并打印它的假设。

两条容易搞错的地方：

- **不能把 in/out token 直接相加。** output 的单价大约是 input 的 5 倍，直接相加
  会严重低估节省 —— 而节省几乎全部来自"不用把答案一个 token 一个 token 写出来"。
- **参数和结果两条路都要付。** agent 自己算，参数在上下文里、结果要写出来；
  调用缓存函数，参数写进工具调用、结果作为工具返回值回到上下文。两边都付，
  所以它们**不是**节省的来源，抵消掉就好。真正省下的是重新想一遍这件事的成本。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


def tokens_of(value: Any) -> int:
    """JSON 值折成 token 数的粗估。4 字符 ≈ 1 token，中文偏保守。"""
    if value is None:
        return 0
    blob = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False,
                                                           default=str)
    return max(1, len(blob) // 4)


@dataclass(frozen=True)
class CostModel:
    """把一次调用折算成一个可比较的数。

    `out_weight` 和 `reasoning_tokens` 是全部的假设，没有第三个。
    """

    # output token 单价约为 input 的 5 倍（各家各代都在 4~5 之间）。
    out_weight: float = 5.0

    # agent 自己做一遍时，除了读输入写输出，还要**重新想一遍这件事怎么做** ——
    # 读需求、拆步骤、逐条核对。这是节省的主要来源，也是全篇最不确定的一个数。
    # 600 是拍的，等真实轨迹来标定（NEXT.md §5）。
    reasoning_tokens: int = 600

    # 一次工具调用本身的开销：函数名、handle、协议壳子。
    call_overhead_tokens: int = 40

    def weigh(self, input_tokens: float, output_tokens: float) -> float:
        return input_tokens + output_tokens * self.out_weight

    def baseline(self, requirement: str, args: Any, result: Any) -> float:
        """agent 自己做一次。需求要读进去，答案要一个 token 一个 token 写出来。"""
        return self.weigh(tokens_of(requirement) + tokens_of(args),
                          self.reasoning_tokens + tokens_of(result))

    def cached(self, args: Any, result: Any) -> float:
        """走缓存函数一次。参数写进工具调用（output），结果读回来（input）。"""
        return self.weigh(tokens_of(result),
                          self.call_overhead_tokens + tokens_of(args))

    def saving(self, requirement: str, args: Any, result: Any) -> float:
        """单次调用的净节省。

        **可能是负的**，而且这不是公式写错了：参数要一个 token 一个 token 地写进
        工具调用（按 output 计价），而 agent 自己算的时候参数已经在上下文里了
        （按 input 计价）。所以参数特别大、计算又特别简单的函数，调它比自己算还贵。
        这种情况该看得见，不该被公式吃掉。
        """
        return self.baseline(requirement, args, result) - self.cached(args, result)

    def synth(self, input_tokens: int, output_tokens: int) -> float:
        """合成一次的成本。thinking token 计在 output 里，API 也是这么算的。"""
        return self.weigh(input_tokens, output_tokens)

    def assumptions(self) -> str:
        return (f"假设：output 单价 = input×{self.out_weight:g}；"
                f"agent 自己做一次的推理开销 = {self.reasoning_tokens} token（**拍的**）")


DEFAULT = CostModel()


@dataclass
class Ledger:
    """一个函数的收支。合成成本按版本累加 —— 重新合成过的函数回本要更久，
    这笔账不能因为换了版本就一笔勾销。"""

    saved: float = 0.0
    synth_cost: float = 0.0
    calls: int = 0
    ok: int = 0

    @property
    def net_savings(self) -> float:
        return self.saved - self.synth_cost

    @property
    def per_call(self) -> float:
        return self.saved / self.ok if self.ok else 0.0

    @property
    def amortization_point(self) -> float | None:
        """平均调用几次回本。目标 < 5（design.md §9）。"""
        if self.per_call <= 0:
            return None
        return self.synth_cost / self.per_call

    def render(self, cost: CostModel = DEFAULT) -> str:
        pt = self.amortization_point
        lines = [
            f"调用 {self.calls} 次（成功 {self.ok}）",
            f"累计省下   {self.saved:>12,.0f}  加权 token",
            f"合成成本   {self.synth_cost:>12,.0f}",
            f"net_savings{self.net_savings:>12,.0f}  "
            + ("← 已转正" if self.net_savings > 0 else "← 还是净亏"),
        ]
        if pt is not None:
            lines.append(f"回本点     {pt:>12,.1f}  次调用"
                         + ("" if pt < 5 else "　← 目标是 < 5"))
        else:
            lines.append("回本点     　　　　　  算不出来：单次调用并不省钱")
        lines.append(cost.assumptions())
        return "\n".join(lines)
