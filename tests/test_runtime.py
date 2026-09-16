"""call_function 的测试 —— 入参 guard → 沙箱 → 返回 schema。

第一性质（design.md §4.2）：**永远不抛给调用方一个"看起来成功但其实错了"的结果。**
下面每一条都在问同一件事的一个侧面：什么该拦，以及批量里出事怎么归因。
"""
import pytest

from agentjit import Example, Level, Registry, Report, Runtime, Sandbox, Spec
from agentjit.runtime import UnknownHandle
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


def install(rt, code=GOOD, *, examples=EXAMPLES, spec=SPEC):
    return rt.reg.put(REQ, spec, code, _report(), examples).handle


# --- 正常路径 --------------------------------------------------------------
def test_call_returns_the_result(rt):
    h = install(rt)
    out = rt.call(h, ARGS)
    assert out.ok and out.version == "v1"
    assert out.result == [{"name": "y", "rank": 1}, {"name": "x", "rank": 2}]


def test_calling_never_writes_to_disk(rt):
    """调用是纯读。早先它要记调用次数、算省了多少 token、连续失败就隔离版本 ——
    那套东西让"调一次函数"变成了有状态操作，而它需要真实流量才有意义。"""
    h = install(rt)
    before = sorted(p.stat().st_mtime_ns for p in rt.reg.root.rglob("*") if p.is_file())
    rt.call_many(h, [ARGS] * 3)
    assert sorted(p.stat().st_mtime_ns for p in rt.reg.root.rglob("*") if p.is_file()) == before


def test_unknown_handle_is_an_error_not_a_none(rt):
    with pytest.raises(UnknownHandle):
        rt.call("fn_deadbeef", ARGS)


# --- 入参 guard ------------------------------------------------------------
def test_bad_input_is_rejected_before_the_sandbox(rt):
    h = install(rt)
    out = rt.call(h, {"records": "不是数组"})

    assert not out.ok and out.kind == "guard_failed"
    assert "param_schema" in out.message




# --- 算得到实现头上的失败 --------------------------------------------------
BOOM = 'def solve(params, ctx):\n    return [1 / len(params["records"])]\n'


def test_runtime_error_is_reported_not_swallowed(rt):
    h = install(rt, BOOM)
    out = rt.call(h, {"records": []})
    assert not out.ok and out.kind == "runtime_error" and "ZeroDivision" in out.message


def test_return_schema_violation_blocks(rt):
    """返回 schema 不是猜的，是从例子结构反推的。所以它阻断 ——
    放一个不合契约的结果出去，就是"看起来成功但其实错了"。"""
    h = install(rt, 'def solve(params, ctx):\n    return {"nope": 1}\n')
    out = rt.call(h, ARGS)
    assert not out.ok and out.kind == "postcondition_failed"





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


