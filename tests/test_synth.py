"""合成循环的测试 —— 全部用脚本客户端，不打网络、不花 token、结果确定。

这里测的是**循环本身**的行为：反馈有没有带上具体失败、三次不过会不会停、
回复里没代码块会不会被喂回去。
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
    assert [a.gate for a in r.attempts] == ["static", "examples", ""]
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
    assert "examples" in r.reason


def test_missing_code_block_is_fed_back(sb):
    r, client = run(["我建议你自己写一下这个函数。", CORRECT], sb)
    assert r.ok
    assert r.attempts[0].gate == "no_code"
    assert "找不到" in client.calls[1]["user"]


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
