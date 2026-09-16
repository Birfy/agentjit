"""Tests for the three-level lookup.

Two criteria, the second harder than the first:

1. three different wordings of one requirement all land on the same function
2. a requirement that reads almost the same but behaves differently ("sum" vs
   "average") **does not** hit by mistake

The second is the hard one: the two sentences are nearly identical as text, so lexical
retrieval is bound to rank the wrong one first. What stops it is not retrieval — it is
re-verification.
"""
import pytest

from jitagent import Example, Level, Registry, Report, Sandbox, Spec
from jitagent.jit import NeedsClient, compile_function, search_functions
from jitagent.llm import ScriptedClient
from jitagent.lookup import find, schema_compatible, similarity
from jitagent.synth import spec_for
from jitagent.types import GateResult

SUM_REQ = ("Group CSV rows by the type field and sum the amount, returning "
           "{type: total}. The amount may carry a currency symbol and thousands "
           "separators, so clean it.")

# Three ways of saying the same thing. None is a light edit of SUM_REQ; each is a
# rewrite.
SAME_THING = [
    "Bucket the rows on type, add the amounts up, and output one total per type. "
    "Strip the currency symbol and the commas out of each amount first.",
    "For every row, use type as the grouping key and accumulate amount once it has "
    "been cleaned into a number; the result is {type: sum}.",
    "I need a function that totals amount per type, where amount arrives as a string "
    "with a dollar sign and thousands separators.",
]

# Nearly identical to SUM_REQ as text, completely different in behaviour
AVG_REQ = ("Group CSV rows by the type field and average the amount, returning "
           "{type: mean}.")

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

RANK_REQ = ("Rank {name, score} records from highest score to lowest, "
            "ties sharing a rank.")
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
    """A registry that already holds the "sum" function."""
    r = Registry(tmp_path / "registry")
    report = Report(level=Level.VERIFIED, gates=[GateResult("static", True, "passed")])
    r.put(SUM_REQ, spec_for(SUM_REQ, EXAMPLES), SUM_CODE, report, EXAMPLES)
    return r


def fenced(code):
    return f"Here you go.\n\n```python\n{code}\n```\n"


# --- similarity ----------------------------------------------------------------
def test_lexical_similarity_cannot_tell_sum_from_average():
    """This is not testing a feature; it is pinning down a premise.

    "average" overlaps "sum" as text more than any honest rewrite does. So **lexical
    retrieval is bound to rank the wrong one first** — re-verification is not a nice
    extra, it is the only thing that stops this. design.md §6.1 notes that embeddings
    put them just as close together, so switching to vectors does not fix it.
    """
    rewrites = max(similarity(SUM_REQ, s) for s in SAME_THING)
    assert similarity(SUM_REQ, AVG_REQ) > rewrites
    # It does separate an unrelated requirement from a rewrite, which is what the
    # threshold is for — ranking and cost, not correctness.
    assert similarity(SUM_REQ, RANK_REQ) < min(
        similarity(SUM_REQ, s) for s in SAME_THING)


def test_similarity_is_symmetric_and_bounded():
    assert similarity(SUM_REQ, SUM_REQ) == 1.0
    assert similarity(SUM_REQ, AVG_REQ) == similarity(AVG_REQ, SUM_REQ)
    assert similarity("", "") == 1.0 and similarity("abc", "") == 0.0


# --- schema compatibility -----------------------------------------------------------
def test_schema_filter_uses_the_same_ruler_as_the_runtime_guard(reg):
    """Lookup saying "compatible" and the guard then rejecting the call is the
    nastiest kind of self-contradiction to debug. So both sides validate this run's
    examples against the candidate's schema."""
    fn = reg.all()[0]
    assert schema_compatible(fn.spec, EXAMPLES) == ""
    why = schema_compatible(fn.spec, RANK_EXAMPLES)
    assert "param_schema" in why


# --- L1 --------------------------------------------------------------------
def test_exact_requirement_hits_l1(reg, sb):
    lk = find(reg, SUM_REQ, spec_for(SUM_REQ, EXAMPLES), EXAMPLES, sandbox=sb)
    assert lk.level == "L1" and lk.hit


# The caller changed their mind: record a fee as an absolute value, no minus sign.
# Not one word of the requirement text changed.
CORRECTED = EXAMPLES[:-1] + [
    Example({"rows": [{"type": "fee", "amount": "-$25.50"}]}, {"fee": 25.5}, boundary=True)]
ABS_CODE = SUM_CODE.replace("            value = float(cleaned)",
                            "            value = abs(float(cleaned))")


def test_l1_still_reverifies_so_a_changed_expectation_is_not_served_stale(reg, sb):
    """The same requirement text, but this run's examples differ from last time's
    (the old expectation was wrong, or the requirement has been re-understood). Serving
    the stored version means shipping an implementation already known not to satisfy
    the criteria being asked for."""
    lk = find(reg, SUM_REQ, spec_for(SUM_REQ, EXAMPLES), CORRECTED, sandbox=sb)

    assert not lk.hit and lk.level == "miss"
    assert lk.stale is not None, ("this has to read as 'the stored version of this "
                                 "requirement went stale', not as 'never seen'")
    assert "disagrees" in lk.candidates[0].why


# --- L2 --------------------------------------------------------------------
@pytest.mark.parametrize("phrasing", SAME_THING)
def test_three_phrasings_all_land_on_the_same_function(reg, sb, phrasing):
    lk = find(reg, phrasing, spec_for(phrasing, EXAMPLES), EXAMPLES, sandbox=sb)
    assert lk.hit and lk.level == "L2", lk.render()
    assert lk.fn.handle == reg.all()[0].handle


def test_a_near_identical_requirement_with_different_behaviour_misses(reg, sb):
    """Criterion 2. Retrieval ranks it first; re-verification knocks it out."""
    lk = find(reg, AVG_REQ, spec_for(AVG_REQ, AVG_EXAMPLES), AVG_EXAMPLES, sandbox=sb)

    assert not lk.hit, lk.render()
    assert lk.candidates and lk.candidates[0].verdict == "reverify"
    assert "disagrees" in lk.candidates[0].why


def test_an_unrelated_requirement_never_costs_a_sandbox_run(reg, sb):
    """The lexical floor is script-dependent: in Chinese an unrelated requirement
    scores ~0.03 and never becomes a candidate, while in English the bigram floor alone
    puts it around 0.25 — above MIN_SIMILARITY. So the threshold cannot be what keeps
    the cost down. The schema check is: it rejects on the examples, before the sandbox.
    """
    lk = find(reg, RANK_REQ, spec_for(RANK_REQ, RANK_EXAMPLES), RANK_EXAMPLES, sandbox=sb)
    assert not lk.hit
    assert all(c.verdict == "schema" for c in lk.candidates), lk.render()



def test_reverify_needs_examples_to_have_anything_to_say(reg, sb):
    """No examples means no criteria, and no criteria means there is nothing to
    re-verify against — so L2 can only miss."""
    lk = find(reg, SAME_THING[0], spec_for(SAME_THING[0], EXAMPLES), [], sandbox=sb)
    assert not lk.hit


# --- end to end ----------------------------------------------------------------
# The tests below pass gen_tests=0: what they check is the cache path, not test
# generation. Leaving it on would mean every ScriptedClient needs an extra canned reply,
# and the point of each test drowns in unrelated script.
def test_second_compile_costs_no_tokens(reg, sb):
    client = ScriptedClient([fenced(SUM_CODE)])
    r = compile_function(SAME_THING[0], EXAMPLES, client=client, registry=reg, sandbox=sb)

    assert r.ok and r.cache == "hit"
    assert client.calls == [], "asking the model after a hit makes the lookup pointless"
    assert r.tokens == (0, 0)


def test_a_cache_hit_thickens_the_test_set(reg, sb):
    """A hit thickens the function on the way past: this run's examples just passed
    re-verification, so they are real criteria the current implementation agrees with.
    It is the cheapest source of monotonic growth the test set has."""
    fn = reg.all()[0]
    before = len(fn.tests.examples)
    fresh = Example({"rows": [{"type": "tip", "amount": "$7"}]}, {"tip": 7.0})

    compile_function(SAME_THING[0], EXAMPLES + [fresh], registry=reg, sandbox=sb,
                     client=ScriptedClient([]))

    after = reg.get(fn.handle)
    assert len(after.tests.examples) == before + 1
    assert after.tests.examples[-1].origin == "reverify"


def test_missing_the_cache_without_a_client_is_an_error_not_a_silent_none(reg, sb):
    with pytest.raises(NeedsClient):
        compile_function(RANK_REQ, RANK_EXAMPLES, registry=reg, sandbox=sb)


def test_force_new_skips_the_lookup(reg, sb):
    client = ScriptedClient([fenced(SUM_CODE)])
    r = compile_function(SUM_REQ, EXAMPLES, client=client, registry=reg, sandbox=sb,
                         cache="force_new", gen_tests=0)
    assert r.ok and r.cache == "miss" and len(client.calls) == 1
    assert [v.name for v in reg.get(r.handle).versions] == ["v1", "v2"]


def test_ephemeral_runs_but_never_reaches_disk(reg, sb):
    client = ScriptedClient([fenced(RANK_CODE)])
    r = compile_function(RANK_REQ, RANK_EXAMPLES, client=client, registry=reg,
                         sandbox=sb, cache="ephemeral", gen_tests=0)
    assert r.ok and r.handle == "" and len(reg.all()) == 1


def test_stale_l1_produces_a_new_version_of_the_same_function(reg, sb):
    """Same requirement text, different examples — that is a new version of the same
    function, not a new function."""
    r = compile_function(SUM_REQ, CORRECTED, client=ScriptedClient([fenced(ABS_CODE)]),
                         registry=reg, sandbox=sb, gen_tests=0)

    assert r.ok and r.cache == "reused_with_new_version", r.render()
    assert len(reg.all()) == 1
    assert [v.name for v in reg.get(r.handle).versions] == ["v1", "v2"]


# --- search ----------------------------------------------------------------
def test_search_ranks_without_verifying(reg):
    hits = search_functions("group rows by type and total the amount", registry=reg)
    assert hits and hits[0].fn.handle == reg.all()[0].handle
    assert hits[0].verdict == "pending", ("search does not re-verify — whoever calls it\n"
                                      "         has not written the examples yet")
