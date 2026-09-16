"""Tests for call_function — input guard -> sandbox -> return schema.

The first property (design.md §4.2): **never hand the caller a result that looks like a
success but is wrong.** Every test below asks one facet of the same question: what gets
stopped, and who gets blamed when something goes wrong inside a batch.
"""
import pytest

from agentjit import Example, Level, Registry, Report, Runtime, Sandbox, Spec
from agentjit.runtime import UnknownHandle
from agentjit.types import GateResult

REQ = ("Rank {name, score} records by descending score. "
       "Records with the same score share a rank.")

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
    return Report(level=Level.VERIFIED, gates=[GateResult("static", True, "passed")])


@pytest.fixture
def rt(tmp_path):
    return Runtime(Registry(tmp_path / "registry"), Sandbox())


def install(rt, code=GOOD, *, examples=EXAMPLES, spec=SPEC):
    return rt.reg.put(REQ, spec, code, _report(), examples).handle


# --- the happy path --------------------------------------------------------------
def test_call_returns_the_result(rt):
    h = install(rt)
    out = rt.call(h, ARGS)
    assert out.ok and out.version == "v1"
    assert out.result == [{"name": "y", "rank": 1}, {"name": "x", "rank": 2}]


def test_calling_never_writes_to_disk(rt):
    """Calling is a pure read. It used to count calls, compute tokens saved, and
    quarantine a version after repeated failures — machinery that made "call a function"
    a stateful operation, and that only means anything under real traffic."""
    h = install(rt)
    before = sorted(p.stat().st_mtime_ns for p in rt.reg.root.rglob("*") if p.is_file())
    rt.call_many(h, [ARGS] * 3)
    assert sorted(p.stat().st_mtime_ns for p in rt.reg.root.rglob("*") if p.is_file()) == before


def test_unknown_handle_is_an_error_not_a_none(rt):
    with pytest.raises(UnknownHandle):
        rt.call("fn_deadbeef", ARGS)


# --- the input guard ------------------------------------------------------------
def test_bad_input_is_rejected_before_the_sandbox(rt):
    h = install(rt)
    out = rt.call(h, {"records": "not an array"})

    assert not out.ok and out.kind == "guard_failed"
    assert "param_schema" in out.message




# --- failures that belong to the implementation --------------------------------------------------
BOOM = 'def solve(params, ctx):\n    return [1 / len(params["records"])]\n'


def test_runtime_error_is_reported_not_swallowed(rt):
    h = install(rt, BOOM)
    out = rt.call(h, {"records": []})
    assert not out.ok and out.kind == "runtime_error" and "ZeroDivision" in out.message


def test_return_schema_violation_blocks(rt):
    """The return schema is not a guess; it is inferred from the shape of the
    examples. So it blocks — letting a result out that breaks the contract is exactly
    "looks like a success but is wrong"."""
    h = install(rt, 'def solve(params, ctx):\n    return {"nope": 1}\n')
    out = rt.call(h, ARGS)
    assert not out.ok and out.kind == "postcondition_failed"





# --- batching ------------------------------------------------------------------
def test_one_poisonous_input_does_not_condemn_its_batch(rt):
    """A timeout takes the whole sandbox process with it. Without a replay, the dozens
    of innocent calls in the same batch are all condemned as budget_exceeded — and the
    wrong attribution costs more than the failure itself."""
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


