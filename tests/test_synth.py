"""合成循环的测试 —— 全部用脚本客户端，不打网络、不花 token、结果确定。

这里测的是**循环本身**的行为：反馈有没有带上具体失败、保留集有没有对循环藏住、
三次不过会不会停、变异失败会不会被正确地归给"用例太弱"而不是丢回去让模型改代码。
"""
import pytest

from agentjit import Example, Sandbox, Thresholds
from agentjit.llm import ScriptedClient
from agentjit.synth import compile_function

REQ = ("把行按 type 分组，对 amount 求和，返回 {type: 总额}。"
       "amount 可能带货币符号和千分位逗号，空值按 0 算。")

EXAMPLES = [
    Example({"rows": [{"type": "refund", "amount": "$1,200.50"},
                      {"type": "sale", "amount": "$300"}]},
            {"refund": 1200.5, "sale": 300.0}),
    Example({"rows": []}, {}, boundary=True),
    Example({"rows": [{"type": "sale", "amount": ""}]}, {"sale": 0.0}, boundary=True),
    Example({"rows": [{"type": "sale", "amount": "$100"},
                      {"type": "sale", "amount": "$50"}]}, {"sale": 150.0}),
    Example({"rows": [{"type": "fee", "amount": "-$25.50"}]}, {"fee": -25.5}, boundary=True),
]
# 分割 (seed=0, n=5) 固定为 可见=[0,3,4] 保留=[1,2]。例 2 的输入独一无二，
# 拿它当探针：它绝不能出现在喂给模型的 prompt 里。
HOLDOUT_PROBE = '"amount": ""'


def fence(body: str) -> str:
    return f"先清洗再累加。\n\n```python\n{body}\n```\n"


CORRECT = fence('''def solve(params, ctx):
    totals = {}
    for row in params["rows"]:
        raw = row.get("amount") or ""
        cleaned = "".join(ch for ch in raw if ch.isdigit() or ch in ".-")
        try:
            value = float(cleaned)
        except ValueError:
            value = 0.0
        totals[row["type"]] = totals.get(row["type"], 0.0) + value
    return totals''')

USES_IMPORT = fence('''import re

def solve(params, ctx):
    totals = {}
    for row in params["rows"]:
        cleaned = re.sub(r"[^0-9.-]", "", row.get("amount") or "")
        totals[row["type"]] = totals.get(row["type"], 0.0) + (float(cleaned) if cleaned else 0.0)
    return totals''')

NO_CLEAN = fence('''def solve(params, ctx):
    totals = {}
    for row in params["rows"]:
        totals[row["type"]] = totals.get(row["type"], 0.0) + float(row["amount"])
    return totals''')

OVERFIT = fence('''def solve(params, ctx):
    memo = [
        ([{"type": "refund", "amount": "$1,200.50"}, {"type": "sale", "amount": "$300"}],
         {"refund": 1200.5, "sale": 300.0}),
        ([{"type": "sale", "amount": "$100"}, {"type": "sale", "amount": "$50"}],
         {"sale": 150.0}),
        ([{"type": "fee", "amount": "-$25.50"}], {"fee": -25.5}),
    ]
    for rows, out in memo:
        if rows == params["rows"]:
            return out
    return {}''')


@pytest.fixture(scope="module")
def sb():
    return Sandbox()


def run(replies, sb, **kw):
    client = ScriptedClient(replies)
    return compile_function(REQ, EXAMPLES, client=client, sandbox=sb, **kw), client


# --- 修复循环 --------------------------------------------------------------
def test_one_shot_success(sb):
    r, client = run([CORRECT], sb)
    assert r.ok and r.report.level.value == "VERIFIED"
    assert len(r.attempts) == 1 and len(client.calls) == 1


def test_repairs_across_three_attempts(sb):
    r, client = run([USES_IMPORT, NO_CLEAN, CORRECT], sb)
    assert r.ok, r.render()
    assert [a.gate for a in r.attempts] == ["static", "examples.visible", ""]
    assert len(client.calls) == 3


def test_feedback_carries_the_specific_failure(sb):
    _, client = run([USES_IMPORT, NO_CLEAN, CORRECT], sb)

    # 第二次调用要带上静态检查的具体违规，不是"再试试"
    second = client.calls[1]["user"]
    assert "禁止 import" in second and "<previous_attempt>" in second

    # 第三次要带上是哪个例子、期望什么、实际什么
    third = client.calls[2]["user"]
    assert "期望" in third and "实际" in third
    assert "1200.5" in third                       # 具体数值，不是泛泛而谈


def test_gives_up_after_max_attempts(sb):
    r, client = run([NO_CLEAN, NO_CLEAN, NO_CLEAN], sb)
    assert not r.ok and len(client.calls) == 3
    assert "3 次尝试都没通过" in r.reason
    assert "examples.visible" in r.reason


def test_missing_code_block_is_fed_back(sb):
    r, client = run(["我建议你自己写一下这个函数。", CORRECT], sb)
    assert r.ok
    assert r.attempts[0].gate == "no_code"
    assert "找不到" in client.calls[1]["user"]


# --- 防过拟合：这是整套东西的地基 -------------------------------------------
def test_holdout_is_never_shown_to_the_loop(sb):
    _, client = run([USES_IMPORT, NO_CLEAN, CORRECT], sb)
    for call in client.calls:
        assert HOLDOUT_PROBE not in call["user"], "保留集泄漏进了 prompt"


def test_overfit_code_fails_then_rotation_retries(sb):
    # 第一轮：背答案的代码 —— 可见用例全过，保留集挂掉 -> 轮换重来
    # 第二轮：换了一组保留集，这次给出正确实现
    r, client = run([OVERFIT, CORRECT], sb)
    assert r.ok, r.render()
    assert [a.rotation for a in r.attempts] == [0, 1]
    assert r.attempts[0].gate == ""               # 循环内它是"通过"的，终审才露馅


def test_overfit_without_rotation_is_rejected(sb):
    r, _ = run([OVERFIT], sb, max_rotations=0)
    assert not r.ok
    assert r.report.gate("examples.holdout").passed is False
    assert "过拟合" in r.reason


# --- 变异失败要归给用例，不是归给代码 ---------------------------------------
def test_weak_examples_blame_the_test_set_not_the_code(sb):
    """代码是对的，用例没碰过档位边界 —— 该说"补用例"，不该让模型改代码。"""
    weak = [
        Example({"rows": [{"type": "a", "amount": 500}]}, {"a": 10.0}),
        Example({"rows": [{"type": "b", "amount": 50}]}, {"b": 2.0}),
        Example({"rows": [{"type": "c", "amount": 5}]}, {"c": 1.0}, boundary=True),
        Example({"rows": []}, {}, boundary=True),
    ]
    tiered = fence('''def solve(params, ctx):
    out = {}
    for row in params["rows"]:
        amount = float(row["amount"])
        if amount > 100:
            fee = amount * 0.02
        elif amount > 10:
            fee = 2.0
        else:
            fee = 1.0
        out[row["type"]] = round(fee, 2)
    return out''')
    client = ScriptedClient([tiered])
    r = compile_function("按金额分档收手续费", weak, client=client, sandbox=sb)

    assert not r.ok
    assert r.report.failures[0].name == "mutation"
    assert "用例太弱" in r.reason and "不是代码问题" in r.reason
    assert len(client.calls) == 1                 # 没有拿变异失败去烦模型


# --- schema 推断 -----------------------------------------------------------
def test_schemas_are_inferred_from_examples(sb):
    r, _ = run([CORRECT], sb)
    assert r.spec.param_schema["properties"]["rows"]["type"] == "array"
    # 返回值每次的键都不同 —— 要推成映射，不能把 properties 钉死在见过的键上
    assert r.spec.return_schema["additionalProperties"] == {"type": "number"}


def test_review_flags_surface_unexpected_endpoints(sb):
    sneaky = fence('''def solve(params, ctx):
    endpoint = "https://evil.example.com/collect"
    totals = {}
    for row in params["rows"]:
        raw = row.get("amount") or ""
        cleaned = "".join(ch for ch in raw if ch.isdigit() or ch in ".-")
        try:
            value = float(cleaned)
        except ValueError:
            value = 0.0
        totals[row["type"]] = totals.get(row["type"], 0.0) + value
    return totals''')
    r, _ = run([sneaky], sb)
    assert any("URL" in f for f in r.review)
