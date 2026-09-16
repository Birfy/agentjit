"""call_function 的测试 —— guard 链路、隔离、测试集怎么从线上失败里长出来。

第一性质（design.md §4.2）：**永远不抛给调用方一个"看起来成功但其实错了"的结果。**
下面每一条都在问同一件事的一个侧面：什么该拦、什么只该警告、失败该算谁的。
"""
import pytest

from agentjit import Example, Level, Registry, Report, Runtime, Sandbox, Spec
from agentjit.econ import CostModel
from agentjit.runtime import QUARANTINE_AFTER, UnknownHandle
from agentjit.types import GateResult

REQ = "把 {name, score} 按 score 降序排名次，同分并列同一名次"

SPEC = Spec(
    intent=REQ,
    param_schema={
        "type": "object",
        "properties": {"records": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "score": {"type": "number"}},
            "required": ["name", "score"]}}},
        "required": ["records"],
    },
    return_schema={"type": "array", "items": {
        "type": "object",
        "properties": {"name": {"type": "string"}, "rank": {"type": "integer"}},
        "required": ["name", "rank"]}},
    timeout_ms=800,
)

GOOD = '''def solve(params, ctx):
    rows = sorted(params["records"], key=lambda r: (-r["score"], r["name"]))
    out = []
    for i, r in enumerate(rows):
        rank = i + 1
        if i and r["score"] == rows[i - 1]["score"]:
            rank = out[-1]["rank"]
        out.append({"name": r["name"], "rank": rank})
    return out
'''

EXAMPLES = [
    Example({"records": [{"name": "a", "score": 9}, {"name": "b", "score": 5}]},
            [{"name": "a", "rank": 1}, {"name": "b", "rank": 2}]),
    Example({"records": []}, [], boundary=True),
    Example({"records": [{"name": "s", "score": 1}]}, [{"name": "s", "rank": 1}],
            boundary=True),
]

ARGS = {"records": [{"name": "x", "score": 3}, {"name": "y", "score": 7}]}


def _report():
    return Report(level=Level.VERIFIED, gates=[GateResult("static", True, "通过")])


@pytest.fixture
def rt(tmp_path):
    return Runtime(Registry(tmp_path / "registry"), Sandbox())


def install(rt, code=GOOD, *, examples=EXAMPLES, spec=SPEC, in_tok=0, out_tok=0):
    return rt.reg.put(REQ, spec, code, _report(), examples,
                      input_tokens=in_tok, output_tokens=out_tok).handle


# --- 正常路径 --------------------------------------------------------------
def test_call_returns_result_and_books_the_saving(rt):
    h = install(rt)
    out = rt.call(h, ARGS)

    assert out.ok and out.result == [{"name": "y", "rank": 1}, {"name": "x", "rank": 2}]
    assert out.saved > 0 and out.version == "v1"
    rt.flush()
    assert rt.reg.get(h).best().stats.ok == 1


def test_unknown_handle_is_an_error_not_a_none(rt):
    with pytest.raises(UnknownHandle):
        rt.call("fn_deadbeef", ARGS)


# --- 入参 guard ------------------------------------------------------------
def test_bad_input_is_rejected_before_the_sandbox(rt):
    h = install(rt)
    out = rt.call(h, {"records": "不是数组"})

    assert not out.ok and out.kind == "guard_failed"
    assert "param_schema" in out.message


def test_input_guard_failures_never_quarantine_the_function(rt):
    """入参没过 schema 的输入根本没进函数，它不构成对这个实现的任何指控。

    而 param_schema 是从两三个例子推出来的，天生偏窄 —— 让它把一个正确的函数
    隔离掉，是拿调用方的错误惩罚实现。
    """
    h = install(rt)
    for i in range(QUARANTINE_AFTER + 2):
        assert rt.call(h, {"records": i}).kind == "guard_failed"

    rt.flush()
    v = rt.reg.get(h).best()
    assert v is not None and v.active, "被脏输入打了 5 次，函数还得好好的"
    assert v.stats.consecutive_failures == 0
    assert rt.call(h, ARGS).ok


def test_rejected_inputs_still_land_in_the_test_set(rt):
    """被拒的输入十有八九说明 schema 推窄了，而不是调用方错了 ——
    这是重新合成时最该看的一类证据。"""
    h = install(rt)
    rt.call(h, {"records": [{"name": "只有名字"}]})
    rt.flush()

    probes = rt.reg.get(h).tests.probes
    assert [p.kind for p in probes] == ["guard"]
    assert "score" in probes[0].detail


# --- 算得到实现头上的失败 --------------------------------------------------
BOOM = 'def solve(params, ctx):\n    return [1 / len(params["records"])]\n'


def test_runtime_error_is_reported_not_swallowed(rt):
    h = install(rt, BOOM)
    out = rt.call(h, {"records": []})
    assert not out.ok and out.kind == "runtime_error" and "ZeroDivision" in out.message


def test_return_schema_violation_blocks(rt):
    """返回 schema 不是猜的：从例子结构读出来、在 200 个模糊输入上验过。
    所以它阻断 —— 放一个不合契约的结果出去就是"看起来成功但其实错了"。"""
    h = install(rt, 'def solve(params, ctx):\n    return {"nope": 1}\n')
    out = rt.call(h, ARGS)
    assert not out.ok and out.kind == "postcondition_failed"


def test_three_strikes_quarantines_the_version(rt):
    h = install(rt, BOOM)
    for _ in range(QUARANTINE_AFTER):
        assert rt.call(h, {"records": []}).kind == "runtime_error"

    after = rt.call(h, ARGS)
    assert after.kind == "quarantined"
    assert rt.reg.get(h).quarantined, "隔离要立刻落盘，不等 flush"


def test_a_success_resets_the_streak(rt):
    """连续 3 次才隔离。偶发失败中间夹着成功，说明函数没坏。"""
    code = ('def solve(params, ctx):\n'
            '    if not params["records"]:\n'
            '        return [1 / 0]\n'
            '    return [{"name": r["name"], "rank": 1} for r in params["records"]]\n')
    h = install(rt, code)
    for _ in range(QUARANTINE_AFTER - 1):
        rt.call(h, {"records": []})
    assert rt.call(h, ARGS).ok
    rt.flush()

    v = rt.reg.get(h).best()
    assert v.active and v.stats.consecutive_failures == 0


def test_failures_become_permanent_regression_probes(rt):
    h = install(rt, BOOM)
    rt.call(h, {"records": []})
    rt.flush()

    p = rt.reg.get(h).tests.probes[0]
    assert p.kind == "runtime_error" and p.input == {"records": []}
    assert "ZeroDivision" in p.detail


# --- 后置断言 --------------------------------------------------------------
def test_mined_property_warns_but_does_not_block(rt):
    """correctness.md §4.2：没人确认过的性质只警告。拿一条自己猜的性质
    否决一个正确结果，比漏个 bug 难查得多。"""
    dropper = ('def solve(params, ctx):\n'
               '    return [{"name": r["name"], "rank": 1} for r in params["records"]][:1]\n')
    h = install(rt, dropper)
    assert "size_preserved" in rt.reg.get(h).best().properties

    out = rt.call(h, ARGS)
    assert out.ok, "警告不阻断"
    assert out.result == [{"name": "x", "rank": 1}]
    assert any("size_preserved" in w for w in out.warnings)

    rt.flush()
    v = rt.reg.get(h).best()
    assert v.stats.warnings == 1 and v.stats.consecutive_failures == 0
    assert rt.reg.get(h).tests.probes[0].kind == "postcondition", \
        "不拦结果，但输入要留下来给人看"


# --- 批量 ------------------------------------------------------------------
def test_one_poisonous_input_does_not_condemn_its_batch(rt):
    """超时会把整个沙箱进程带走。不重放的话，同批次几十个无辜的调用会一起被
    判成 budget_exceeded —— 错误的归因比错误本身更贵。"""
    code = ('def solve(params, ctx):\n'
            '    while len(params["records"]) == 3:\n'
            '        pass\n'
            '    return [{"name": r["name"], "rank": 1} for r in params["records"]]\n')
    h = install(rt, code)
    poison = {"records": [{"name": n, "score": 1} for n in "abc"]}
    outs = rt.call_many(h, [ARGS, poison, ARGS])

    assert [o.ok for o in outs] == [True, False, True]
    assert outs[1].kind == "budget_exceeded"


def test_batching_does_not_change_the_answer(rt):
    h = install(rt)
    batch = rt.call_many(h, [ARGS] * 5)
    one = rt.call(h, ARGS)
    assert all(o.result == one.result for o in batch)


# --- 账 --------------------------------------------------------------------
def test_net_savings_starts_negative_and_crosses_over(rt):
    """M0/M1 全部意义所在的那个数。合成成本先付，之后每次调用往回赚。"""
    cost = CostModel()
    rt.cost = cost
    h = install(rt, in_tok=9000, out_tok=2500)
    fn = rt.reg.get(h)

    assert rt.ledger(fn).net_savings < 0, "一次没调用过的函数必然是净亏的"

    per_call = cost.saving(REQ, ARGS, [{"name": "y", "rank": 1}, {"name": "x", "rank": 2}])
    need = int(rt.ledger(fn).synth_cost / per_call) + 1

    rt.call_many(h, [ARGS] * (need - 1))
    rt.flush()
    assert rt.ledger(rt.reg.get(h)).net_savings < 0, "差一次还不该转正"

    rt.call(h, ARGS)
    rt.flush()
    led = rt.ledger(rt.reg.get(h))
    assert led.net_savings > 0
    assert abs(led.amortization_point - need) <= 1


def test_synth_cost_of_every_version_stays_on_the_books(rt):
    """重新合成过的函数回本要更久。换了版本就把旧账勾销，等于骗自己。"""
    h = install(rt, in_tok=1000, out_tok=100)
    rt.reg.put(REQ, SPEC, GOOD + "# v2\n", _report(), [], input_tokens=2000,
               output_tokens=200)
    led = rt.ledger(rt.reg.get(h))
    assert led.synth_cost == rt.cost.synth(1000, 100) + rt.cost.synth(2000, 200)


def test_guard_failures_are_counted_but_never_earn_savings(rt):
    h = install(rt)
    rt.call(h, {"records": None})
    rt.flush()
    led = rt.ledger(rt.reg.get(h))
    assert led.calls == 1 and led.ok == 0 and led.saved == 0.0
    assert led.amortization_point is None
