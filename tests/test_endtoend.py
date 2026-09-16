"""闭环：一段话 → 合成 → 入库 → 按名字取代码 / 反复调用。

全程用脚本客户端，不打网络、不花 token —— 这里证明的是管道通了，
不是"Haiku 真的几次能修对"。
"""
import pytest

from agentjit import Example, Registry, Runtime, Sandbox, get_code
from agentjit.llm import ScriptedClient
from agentjit.synth import compile_function

REQ = ("把行按 type 分组，对 amount 求和，返回 {type: 总额}。"
       "amount 可能带货币符号和千分位逗号，空值按 0 算。")

EXAMPLES = [
    Example({"rows": [{"type": "refund", "amount": "$1,200.50"},
                      {"type": "sale", "amount": "$300"}],},
            {"refund": 1200.5, "sale": 300.0}),
    Example({"rows": []}, {}, boundary=True),
    Example({"rows": [{"type": "sale", "amount": ""}]}, {"sale": 0.0}, boundary=True),
    Example({"rows": [{"type": "sale", "amount": "$100"},
                      {"type": "sale", "amount": "$50"}]}, {"sale": 150.0}),
    Example({"rows": [{"type": "fee", "amount": "-$25.50"}]}, {"fee": -25.5}, boundary=True),
]

CORRECT = '''先清洗再累加。

```python
def solve(params, ctx):
    totals = {}
    for row in params["rows"]:
        raw = row.get("amount") or ""
        cleaned = "".join(ch for ch in raw if ch.isdigit() or ch in ".-")
        try:
            value = float(cleaned)
        except ValueError:
            value = 0.0
        totals[row["type"]] = totals.get(row["type"], 0.0) + value
    return totals
```
'''


@pytest.fixture(scope="module")
def sb():
    return Sandbox()


def test_compile_then_get_and_call_by_name(tmp_path, sb):
    reg = Registry(tmp_path / "registry")
    rt = Runtime(reg, sb)

    r = compile_function(REQ, EXAMPLES, client=ScriptedClient([CORRECT]), sandbox=sb)
    assert r.ok, r.render()
    fn = reg.put(REQ, r.spec, r.code, r.report, EXAMPLES, name="group_sum",
                 model="scripted", attempts=len(r.attempts))

    # 按名字取代码
    assert "def solve" in get_code("group_sum", registry=reg)
    assert reg.get("group_sum").spec_hash == fn.spec_hash

    # 按名字反复调用
    args = {"rows": [{"type": "sale", "amount": f"${i}"} for i in range(1, 6)]}
    outs = rt.call_many("group_sum", [args] * 200)
    assert all(o.ok for o in outs)
    assert outs[0].result == {"sale": 15.0}

    # 落盘的是用例，不只是代码
    back = reg.get("group_sum")
    assert len(back.tests.examples) == len(EXAMPLES)
    assert back.best().code.strip() == r.code.strip()
    assert back.best().report.level.value == "VERIFIED"



def test_recompiling_the_same_requirement_adds_a_version_not_a_function(tmp_path, sb):
    """同一个需求再编译一次，应该长出 v2，而不是第二个函数 ——
    否则测试集会分叉成两份，"模型升级 = 免费的全库重生成"就没了依托。"""
    reg = Registry(tmp_path / "registry")
    for _ in range(2):
        r = compile_function(REQ, EXAMPLES, client=ScriptedClient([CORRECT]), sandbox=sb)
        fn = reg.put(REQ, r.spec, r.code, r.report, EXAMPLES)

    assert len(reg.all()) == 1
    assert [v.name for v in reg.get(fn.handle).versions] == ["v1", "v2"]
    assert len(reg.get(fn.handle).tests.examples) == len(EXAMPLES)
