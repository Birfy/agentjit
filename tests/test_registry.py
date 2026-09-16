"""Registry 的测试 —— 落盘、选版本、测试集怎么长。

这里不碰 LLM，也不碰沙箱：registry 只管存和选，存错了选错了，上面再对也白搭。
"""
import json

import pytest

from agentjit import Example, Level, Report, Spec
# TestSet 别名成 Suite：pytest 会去收集任何叫 Test* 的类，然后抱怨它有 __init__
from agentjit.registry import MAX_PROBES_PER_KIND, NotCacheable, Probe, Registry
from agentjit.registry import TestSet as Suite
from agentjit.registry import spec_hash
from agentjit.types import GateResult

REQ = "把行按 type 分组对 amount 求和"
CODE = "def solve(params, ctx):\n    return {'n': len(params['rows'])}\n"

SPEC = Spec(intent=REQ,
            param_schema={"type": "object",
                          "properties": {"rows": {"type": "array", "items": {"type": "object"}}},
                          "required": ["rows"]},
            return_schema={"type": "object"})

EXAMPLES = [
    Example({"rows": [{"type": "a"}]}, {"n": 1}),
    Example({"rows": []}, {"n": 0}, boundary=True),
]


def report(level=Level.VERIFIED):
    return Report(level=level, gates=[GateResult("static", True, "通过")], wall_ms=1.0)


@pytest.fixture
def reg(tmp_path):
    return Registry(tmp_path / "registry")


# --- spec_hash -------------------------------------------------------------
def test_hash_ignores_whitespace_but_not_meaning():
    a = spec_hash("把行按 type 分组\n对 amount 求和", SPEC)
    b = spec_hash("把行按 type   分组 对 amount 求和  ", SPEC)
    assert a == b, "换行和多余空格不该换一个函数出来"
    assert spec_hash("求平均", SPEC) != a


def test_hash_covers_schema_not_just_text():
    """同一句'求和'，输入结构不同就是两个函数 —— 只 hash 文本会把它们混成一个。"""
    other = Spec(intent=REQ, param_schema={"type": "object",
                                           "properties": {"values": {"type": "array"}}},
                 return_schema={"type": "object"})
    assert spec_hash(REQ, SPEC) != spec_hash(REQ, other)


# --- 落盘 ------------------------------------------------------------------
def test_put_then_get_roundtrip(reg):
    fn = reg.put(REQ, SPEC, CODE, report(), EXAMPLES, model="m", input_tokens=100,
                 output_tokens=20)
    back = reg.get(fn.handle)

    assert back is not None
    assert back.requirement == REQ
    assert back.spec.param_schema == SPEC.param_schema
    assert [e.input for e in back.tests.examples] == [e.input for e in EXAMPLES]
    v = back.best()
    assert v.name == "v1" and v.code == CODE and v.level is Level.VERIFIED
    assert (v.synth_input_tokens, v.synth_output_tokens) == (100, 20)
    assert v.report.gate("static").passed


def test_get_accepts_truncated_handle(reg):
    fn = reg.put(REQ, SPEC, CODE, report(), EXAMPLES)
    assert reg.get(fn.handle[:9]).spec_hash == fn.spec_hash


def test_ephemeral_never_reaches_disk(reg):
    """EPHEMERAL 的定义就是拿不出判据。存下来等于把没人验过的实现摆上货架。"""
    with pytest.raises(NotCacheable):
        reg.put(REQ, SPEC, CODE, report(Level.EPHEMERAL), EXAMPLES)
    assert reg.all() == []


def test_writes_are_atomic_json(reg):
    fn = reg.put(REQ, SPEC, CODE, report(), EXAMPLES)
    for name in ("spec.json", "tests.json"):
        json.loads((reg.dir_of(fn.spec_hash) / name).read_text())
    assert not list(reg.dir_of(fn.spec_hash).glob(".tmp-*"))


# --- 版本 ------------------------------------------------------------------
def test_second_put_adds_a_version_and_keeps_one_test_set(reg):
    reg.put(REQ, SPEC, CODE, report(), EXAMPLES)
    fn = reg.put(REQ, SPEC, CODE + "# v2\n", report(),
                 [Example({"rows": [{"type": "b"}, {"type": "c"}]}, {"n": 2})])

    back = reg.get(fn.handle)
    assert [v.name for v in back.versions] == ["v1", "v2"]
    # 测试集属于 spec 不属于版本：新版本带来的例子进的是同一份
    assert len(back.tests.examples) == 3
    assert not list(reg.dir_of(fn.spec_hash).glob("v*/tests.json"))


def test_best_prefers_level_then_reverify_then_failure_rate(reg):
    fn = reg.put(REQ, SPEC, CODE, report(), EXAMPLES)
    reg.put(REQ, SPEC, CODE + "# v2\n", report(), [])
    fn = reg.get(fn.handle)

    assert fn.best().name == "v2", "同等条件下取新的"

    v1, v2 = fn.versions
    v1.stats.reverify_passes = 3
    assert fn.best().name == "v1", "复验通过次数更高的赢"

    v2.stats.reverify_passes = 3
    v1.stats.ok, v1.stats.runtime_error = 8, 2
    v2.stats.ok = 10
    assert fn.best().name == "v2", "打平就看谁在线上挂得少"


def test_quarantined_version_is_never_selected(reg):
    fn = reg.put(REQ, SPEC, CODE, report(), EXAMPLES)
    reg.put(REQ, SPEC, CODE + "# v2\n", report(), [])
    fn = reg.get(fn.handle)

    reg.quarantine(fn, fn.version("v2"), "连续挂了 3 次")
    assert fn.best().name == "v1"

    reg.quarantine(fn, fn.version("v1"), "也挂了")
    assert fn.best() is None and fn.quarantined

    back = reg.get(fn.handle)                      # 隔离要立刻落盘，不等 flush
    assert back.best() is None
    assert "连续挂了 3 次" in back.version("v2").quarantine_reason
    assert back.version("v2").code, "隔离不删代码 —— 它是排查线上分歧的证据"


# --- 测试集 ----------------------------------------------------------------
def test_examples_dedup_by_input():
    ts = Suite()
    assert ts.add_example(EXAMPLES[0]) is True
    assert ts.add_example(Example(EXAMPLES[0].input, {"n": 999})) is False
    assert len(ts.examples) == 1


def test_probe_repeats_thicken_the_count_not_the_test_set():
    ts = Suite()
    assert ts.add_probe(Probe({"rows": None}, "guard", "不是数组")) is True
    for _ in range(9):
        assert ts.add_probe(Probe({"rows": None}, "guard", "不是数组")) is False
    assert len(ts.probes) == 1 and ts.probes[0].seen == 10


def test_probe_cap_drops_the_one_offs_first():
    """上限到了先丢只见过一次的。反复被打中的输入才是真信号。"""
    ts = Suite()
    ts.add_probe(Probe({"keep": 1}, "guard", ""))
    for _ in range(5):
        ts.add_probe(Probe({"keep": 1}, "guard", ""))
    for i in range(MAX_PROBES_PER_KIND + 20):
        ts.add_probe(Probe({"noise": i}, "guard", ""))

    guards = [p for p in ts.probes if p.kind == "guard"]
    assert len(guards) == MAX_PROBES_PER_KIND
    assert any(p.input == {"keep": 1} for p in guards)


def test_probe_kinds_have_separate_budgets():
    ts = Suite()
    for i in range(MAX_PROBES_PER_KIND + 10):
        ts.add_probe(Probe({"i": i}, "guard", ""))
    ts.add_probe(Probe({"real": 1}, "runtime_error", "炸了"))
    assert any(p.kind == "runtime_error" for p in ts.probes), \
        "调用方的脏输入再多，也不该把真正的线上事故挤出去"


# --- 名字 ------------------------------------------------------------------
def test_get_by_name(reg):
    """handle 是给机器用的，名字是给人用的。这东西的用法就是
    "上次那个排名次的函数叫什么来着"。"""
    from agentjit.jit import get_code

    fn = reg.put(REQ, SPEC, CODE, report(), EXAMPLES, name="group_sum")
    assert reg.get("group_sum").spec_hash == fn.spec_hash
    assert reg.get(fn.handle).name == "group_sum"
    assert get_code("group_sum", registry=reg).strip() == CODE.strip()
    assert reg.get("没这个名字") is None


def test_a_name_points_at_exactly_one_function(reg):
    """一个名字指向两个函数就等于没索引。宁可在这里报错，
    也不要让 get("rank") 的结果取决于目录遍历顺序。"""
    from agentjit.registry import NameTaken

    reg.put(REQ, SPEC, CODE, report(), EXAMPLES, name="rank")
    other = Spec(intent="别的", param_schema={"type": "object"},
                 return_schema={"type": "object"})
    with pytest.raises(NameTaken):
        reg.put("完全不同的需求", other, CODE, report(), EXAMPLES, name="rank")


def test_renaming_and_later_versions_keep_one_name(reg):
    fn = reg.put(REQ, SPEC, CODE, report(), EXAMPLES, name="old")
    reg.put(REQ, SPEC, CODE + "# v2\n", report(), [], name="new")

    back = reg.get(fn.handle)
    assert back.name == "new" and len(back.versions) == 2
    assert reg.get("new") is not None


def test_a_function_without_a_name_falls_back_to_its_handle(reg):
    fn = reg.put(REQ, SPEC, CODE, report(), EXAMPLES)
    assert fn.ref == fn.handle


def test_quarantined_code_never_comes_out_of_get_code(reg):
    """get_code 取的是 best()。被隔离的版本不该从这个口子流出去。"""
    from agentjit.jit import get_code

    fn = reg.put(REQ, SPEC, CODE, report(), EXAMPLES, name="doomed")
    reg.quarantine(fn, fn.best(), "挂太多次")
    assert get_code("doomed", registry=reg) == ""
