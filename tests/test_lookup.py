"""三级查找的测试。

完成判据（NEXT.md §3）两条，一条比一条要紧：

1. 同一需求换三种说法都命中同一个函数
2. 一个语义相近但行为不同的需求（"求和" vs "求平均"）**不会**误命中

第 2 条才是难的：这两句话字面上几乎一样，词法检索必然把它排在最前面。挡住它的
不是检索，是复验。
"""
import pytest

from agentjit import Example, Level, Registry, Report, Sandbox, Spec
from agentjit.jit import NeedsClient, compile_function, search_functions
from agentjit.llm import ScriptedClient
from agentjit.lookup import find, schema_compatible, similarity
from agentjit.synth import spec_for
from agentjit.types import GateResult

SUM_REQ = ("把 CSV 行按 type 字段分组，对 amount 求和，返回 {type: 总额}。"
           "金额可能带货币符号和千分位逗号，要清洗。")

# 同一件事的三种说法。都不是 SUM_REQ 的改写，是重写。
SAME_THING = [
    "按 type 把行分组，把 amount 加起来，输出每个 type 的总额。amount 里的货币符号和逗号要去掉。",
    "对每一行，用 type 做分组键，amount 清洗成数字后累加，结果是 {type: 总和}。",
    "给我一个按 type 汇总 amount 的函数，amount 是带 $ 和千分位的字符串。",
]

# 字面上和 SUM_REQ 几乎一样，行为完全不同
AVG_REQ = "把 CSV 行按 type 字段分组，对 amount 求平均，返回 {type: 均值}。"

EXAMPLES = [
    Example({"rows": [{"type": "refund", "amount": "$1,200.50"},
                      {"type": "sale", "amount": "$300"}]},
            {"refund": 1200.5, "sale": 300.0}),
    Example({"rows": []}, {}, boundary=True),
    # 这一条别删：它是唯一走到 except ValueError 那条分支的用例，
    # 拿掉之后 SUM_CODE 过不了 coverage.branch
    Example({"rows": [{"type": "sale", "amount": ""}]}, {"sale": 0.0}, boundary=True),
    Example({"rows": [{"type": "sale", "amount": "$100"},
                      {"type": "sale", "amount": "$50"}]}, {"sale": 150.0}),
    Example({"rows": [{"type": "fee", "amount": "-$25.50"}]}, {"fee": -25.5}, boundary=True),
]

AVG_EXAMPLES = [
    Example({"rows": [{"type": "sale", "amount": "$100"},
                      {"type": "sale", "amount": "$50"}]}, {"sale": 75.0}),
    Example({"rows": []}, {}, boundary=True),
    Example({"rows": [{"type": "fee", "amount": "-$25.50"}]}, {"fee": -25.5}, boundary=True),
]

SUM_CODE = '''def solve(params, ctx):
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
'''

RANK_REQ = "把 {name, score} 记录按 score 从高到低排名次，同分并列。"
RANK_CODE = '''def solve(params, ctx):
    rows = sorted(params["records"], key=lambda r: -r["score"])
    return [{"name": r["name"], "rank": i + 1} for i, r in enumerate(rows)]
'''
RANK_EXAMPLES = [
    Example({"records": [{"name": "a", "score": 9}, {"name": "b", "score": 5}]},
            [{"name": "a", "rank": 1}, {"name": "b", "rank": 2}]),
    Example({"records": []}, [], boundary=True),
]


@pytest.fixture(scope="module")
def sb():
    return Sandbox()


@pytest.fixture
def reg(tmp_path, sb):
    """一个已经装了"求和"函数的 registry。"""
    r = Registry(tmp_path / "registry")
    report = Report(level=Level.VERIFIED, gates=[GateResult("static", True, "通过")])
    r.put(SUM_REQ, spec_for(SUM_REQ, EXAMPLES), SUM_CODE, report, EXAMPLES)
    return r


def fenced(code):
    return f"给你。\n\n```python\n{code}\n```\n"


# --- 相似度 ----------------------------------------------------------------
def test_lexical_similarity_cannot_tell_sum_from_average():
    """这条不是在测功能，是在钉住一个前提。

    "求平均"和"求和"的字面重合度，比任何一句真正的改写都高。所以**词法检索
    必然把错的那个排在最前面** —— 复验不是锦上添花，它是唯一挡得住这件事的东西。
    design.md §6.1 说向量也一样近，换成向量并不能解决它。
    """
    rewrites = max(similarity(SUM_REQ, s) for s in SAME_THING)
    assert similarity(SUM_REQ, AVG_REQ) > rewrites
    # 完全不相干的东西倒是能靠词法挡掉，所以阈值仍然有用
    assert similarity(SUM_REQ, RANK_REQ) < 0.1


def test_similarity_is_symmetric_and_bounded():
    assert similarity(SUM_REQ, SUM_REQ) == 1.0
    assert similarity(SUM_REQ, AVG_REQ) == similarity(AVG_REQ, SUM_REQ)
    assert similarity("", "") == 1.0 and similarity("abc", "") == 0.0


# --- schema 兼容 -----------------------------------------------------------
def test_schema_filter_uses_the_same_ruler_as_the_runtime_guard(reg):
    """查找说兼容、调用时被 guard 拦下，是最难查的一类自相矛盾。
    所以两边都拿本次的例子去验候选的 schema。"""
    fn = reg.all()[0]
    assert schema_compatible(fn.spec, EXAMPLES) == ""
    why = schema_compatible(fn.spec, RANK_EXAMPLES)
    assert "param_schema" in why


# --- L1 --------------------------------------------------------------------
def test_exact_requirement_hits_l1(reg, sb):
    lk = find(reg, SUM_REQ, spec_for(SUM_REQ, EXAMPLES), EXAMPLES, sandbox=sb)
    assert lk.level == "L1" and lk.hit


# 调用方改主意了：手续费按绝对值记，不带负号。需求原文一个字没变。
CORRECTED = EXAMPLES[:-1] + [
    Example({"rows": [{"type": "fee", "amount": "-$25.50"}]}, {"fee": 25.5}, boundary=True)]
ABS_CODE = SUM_CODE.replace("            value = float(cleaned)",
                            "            value = abs(float(cleaned))")


def test_l1_still_reverifies_so_a_changed_expectation_is_not_served_stale(reg, sb):
    """同一段需求文本，这次的例子和上次不一样（上次的期望写错了，或者需求被
    重新理解了）。直接用旧版本，就是拿一个已知不满足本次判据的实现去交差。"""
    lk = find(reg, SUM_REQ, spec_for(SUM_REQ, EXAMPLES), CORRECTED, sandbox=sb)

    assert not lk.hit and lk.level == "miss"
    assert lk.stale is not None, "要认出这是'同一个需求的旧版本过时了'，而不是没见过"
    assert "对不上" in lk.candidates[0].why


# --- L2 --------------------------------------------------------------------
@pytest.mark.parametrize("phrasing", SAME_THING)
def test_three_phrasings_all_land_on_the_same_function(reg, sb, phrasing):
    lk = find(reg, phrasing, spec_for(phrasing, EXAMPLES), EXAMPLES, sandbox=sb)
    assert lk.hit and lk.level == "L2", lk.render()
    assert lk.fn.handle == reg.all()[0].handle


def test_a_near_identical_requirement_with_different_behaviour_misses(reg, sb):
    """完成判据的第 2 条。检索会把它排第一，复验把它打掉。"""
    lk = find(reg, AVG_REQ, spec_for(AVG_REQ, AVG_EXAMPLES), AVG_EXAMPLES, sandbox=sb)

    assert not lk.hit, lk.render()
    assert lk.candidates and lk.candidates[0].verdict == "reverify"
    assert "对不上" in lk.candidates[0].why


def test_unrelated_requirements_do_not_even_become_candidates(reg, sb):
    lk = find(reg, RANK_REQ, spec_for(RANK_REQ, RANK_EXAMPLES), RANK_EXAMPLES, sandbox=sb)
    assert not lk.hit and lk.candidates == [], "词法阈值该把它挡在复验之外，省一次沙箱"


def test_a_quarantined_function_is_not_a_candidate(reg, sb):
    fn = reg.all()[0]
    reg.quarantine(fn, fn.best(), "挂太多次了")
    lk = find(reg, SAME_THING[0], spec_for(SAME_THING[0], EXAMPLES), EXAMPLES, sandbox=sb)
    assert not lk.hit


def test_reverify_needs_examples_to_have_anything_to_say(reg, sb):
    """不给例子就没有判据，没有判据就谈不上复验 —— 这时候 L2 只能 miss。
    "调用方不给例子怎么办"的答案是差分裁决（M1），不是放宽这里。"""
    lk = find(reg, SAME_THING[0], spec_for(SAME_THING[0], EXAMPLES), [], sandbox=sb)
    assert not lk.hit


# --- 串起来 ----------------------------------------------------------------
def test_second_compile_costs_no_tokens(reg, sb):
    client = ScriptedClient([fenced(SUM_CODE)])
    r = compile_function(SAME_THING[0], EXAMPLES, client=client, registry=reg, sandbox=sb)

    assert r.ok and r.cache == "hit"
    assert client.calls == [], "命中了还去问模型，就白查了"
    assert r.tokens == (0, 0)


def test_a_cache_hit_thickens_the_test_set(reg, sb):
    """命中顺手把函数变厚：本次的例子刚通过复验，就是和当前实现一致的真判据。
    这是测试集单调增长最便宜的一条来源。"""
    fn = reg.all()[0]
    before = len(fn.tests.examples)
    fresh = Example({"rows": [{"type": "tip", "amount": "$7"}]}, {"tip": 7.0})

    compile_function(SAME_THING[0], EXAMPLES + [fresh], registry=reg, sandbox=sb,
                     client=ScriptedClient([]))

    after = reg.get(fn.handle)
    assert len(after.tests.examples) == before + 1
    assert after.tests.examples[-1].origin == "reverify"
    assert after.best().stats.reverify_passes == 1, "被别人的标准验过一次，可信度记一笔"


def test_missing_the_cache_without_a_client_is_an_error_not_a_silent_none(reg, sb):
    with pytest.raises(NeedsClient):
        compile_function(RANK_REQ, RANK_EXAMPLES, registry=reg, sandbox=sb)


def test_force_new_skips_the_lookup(reg, sb):
    client = ScriptedClient([fenced(SUM_CODE)])
    r = compile_function(SUM_REQ, EXAMPLES, client=client, registry=reg, sandbox=sb,
                         cache="force_new")
    assert r.ok and r.cache == "miss" and len(client.calls) == 1
    assert [v.name for v in reg.get(r.handle).versions] == ["v1", "v2"]


def test_ephemeral_runs_but_never_reaches_disk(reg, sb):
    client = ScriptedClient([fenced(RANK_CODE)])
    r = compile_function(RANK_REQ, RANK_EXAMPLES, client=client, registry=reg,
                         sandbox=sb, cache="ephemeral")
    assert r.ok and r.handle == "" and len(reg.all()) == 1


def test_stale_l1_produces_a_new_version_of_the_same_function(reg, sb):
    """需求原文没变、例子变了 —— 这是同一个函数的新版本，不是新函数。"""
    r = compile_function(SUM_REQ, CORRECTED, client=ScriptedClient([fenced(ABS_CODE)]),
                         registry=reg, sandbox=sb)

    assert r.ok and r.cache == "reused_with_new_version", r.render()
    assert len(reg.all()) == 1
    assert [v.name for v in reg.get(r.handle).versions] == ["v1", "v2"]


# --- search ----------------------------------------------------------------
def test_search_ranks_without_verifying(reg):
    hits = search_functions("按 type 分组求和", registry=reg)
    assert hits and hits[0].fn.handle == reg.all()[0].handle
    assert hits[0].verdict == "pending", "search 不复验 —— 它的调用方还没写例子"
