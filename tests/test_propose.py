"""Tests for automatic test-case generation.

What matters most here is not "can it write cases" but **what happens when it writes a
wrong one**: a case with a wrong expected value condemns a correct implementation, and
it is very hard to debug. So most of this file is about which generated cases get
dropped, and who gets blamed when the only failures are on generated cases.
"""
import json

import pytest

from jitagent import Example, Registry, Sandbox
from jitagent.jit import compile_function
from jitagent.llm import LLMResponse, ScriptedClient
from jitagent.propose import propose_tests

REQ = ("Rank {name, score} records from highest score to lowest. "
       "Records with the same score share a rank.")

SEEDS = [
    Example({"records": [{"name": "a", "score": 9}, {"name": "b", "score": 5}]},
            [{"name": "a", "rank": 1}, {"name": "b", "rank": 2}]),
    Example({"records": []}, [], boundary=True),
]

GOOD_CODE = '''```python
def solve(params, ctx):
    rows = sorted(params["records"], key=lambda r: (-r["score"], r["name"]))
    out = []
    for i, r in enumerate(rows):
        rank = i + 1
        if i and r["score"] == rows[i - 1]["score"]:
            rank = out[-1]["rank"]
        out.append({"name": r["name"], "rank": rank})
    return out
```
'''


def fence(items):
    return ("Here are a few more.\n\n```json\n"
            + json.dumps(items, ensure_ascii=False) + "\n```\n")


# --- assumptions: decisions the model made where the requirement did not --------------------------------
def test_assumes_is_parsed_and_marked():
    """Measurement forced this field into existence. Given a vague "deduplicate a list
    of records", the model wrote "compare whole records", "keep the first" and "preserve
    order" into its cases as settled fact — three decisions the requirement never made.
    Those cases then become criteria, and condemn a correct implementation that read it
    the other way."""
    p = propose(fence([
        {"input": {"records": [{"name": "a", "score": 1}]},
         "output": [{"name": "a", "rank": 1}], "note": "single element"},
        {"input": {"records": [{"name": "b", "score": 2.5}]},
         "output": [{"name": "b", "rank": 3}], "note": "the 0.5 boundary",
         "assumes": "0.5 rounds away from zero"},
    ]))
    assert [bool(e.assumes) for e in p.examples] == [False, True]
    assert p.assumed[0].assumes == "0.5 rounds away from zero"


def test_assumes_survives_a_round_trip_to_disk(tmp_path):
    """An assumption is stored with its case. A year from now, the only thing that can
    answer "why is this the expected value" is this field."""
    from jitagent import Level, Report, Registry, Spec
    from jitagent.types import GateResult

    reg = Registry(tmp_path / "r")
    report = Report(level=Level.VERIFIED, gates=[GateResult("static", True, "passed")])
    ex = Example({"x": 1}, 2, origin="generated",
                 assumes="an empty string does not count as a missing value")
    reg.put("a requirement", Spec("a requirement", {"type": "object"}, {}),
            "def solve(params, ctx):\n    return 2\n", report, [ex], name="f")

    assert (reg.get("f").tests.examples[0].assumes
            == "an empty string does not count as a missing value")


@pytest.fixture(scope="module")
def sb():
    return Sandbox()


def propose(reply, seeds=SEEDS, n=4):
    return propose_tests(REQ, seeds, client=ScriptedClient([reply]), n=n)


# --- parsing ------------------------------------------------------------------
def test_parses_a_json_block():
    p = propose(fence([
        {"input": {"records": [{"name": "s", "score": 1}]},
         "output": [{"name": "s", "rank": 1}], "note": "single element"},
        {"input": {"records": [{"name": "p", "score": 3}, {"name": "q", "score": 3}]},
         "output": [{"name": "p", "rank": 1}, {"name": "q", "rank": 1}],
         "note": "a tie"},
    ]))
    assert [e.origin for e in p.examples] == ["generated", "generated"]
    assert p.examples[1].note == "a tie"


def test_a_bare_array_without_a_fence_still_parses():
    p = propose('[{"input": {"records": []}, "output": []}]')
    # it collides with a seed, so it gets dropped — but that proves parsing worked
    assert p.error == "" and p.dropped


# --- the drop rules: the main defence against a wrong generated case ----------------------------------------
def test_a_generated_case_may_not_overrule_a_seed():
    """The seeds come from the caller, so they are right. A generated case with the
    same input and a different output means the model is editing the caller's answer —
    drop it, and say why."""
    p = propose(fence([{"input": {"records": []}, "output": [{"name": "out of thin air", "rank": 1}]}]))
    assert p.examples == []
    assert "the caller's wins" in p.dropped[0]["why"]


def test_malformed_items_are_dropped_not_crashed_on():
    p = propose(fence([
        {"output": [1]},                                   # no input
        {"input": "not a dict", "output": []},
        {"input": {"records": [{"name": "z", "score": 2}]},
         "output": [{"name": "z", "rank": 1}]},            # this one is fine
    ]))
    assert len(p.examples) == 1 and len(p.dropped) == 2


def test_duplicate_inputs_are_kept_once():
    same = {"input": {"records": [{"name": "z", "score": 2}]},
            "output": [{"name": "z", "rank": 1}]}
    p = propose(fence([same, same]))
    assert len(p.examples) == 1 and len(p.dropped) == 1


def test_unserializable_expectations_are_dropped():
    p = propose(fence([{"input": {"records": []}, "output": float("nan")}]))
    assert p.examples == []


# --- a failure here must not take the compile down ------------------------------------------------------
def test_a_reply_without_json_is_reported_not_raised():
    p = propose("I do not think this requirement needs tests.")
    assert p.examples == [] and "no JSON array" in p.error


def test_a_broken_client_does_not_take_the_whole_compile_down():
    """Generating cases is a bonus. When it fails, fall back to "use only the cases
    the caller supplied" rather than failing the whole compile."""
    class Broken:
        def complete(self, **kw):
            raise RuntimeError("network is down")

    p = propose_tests(REQ, SEEDS, client=Broken(), n=4)
    assert p.examples == [] and "network is down" in p.error


# --- attribution: the single most important thing in this design --------------------------------------------
def test_failing_only_on_generated_cases_is_handed_back_for_adjudication(tmp_path, sb):
    """The code passed every case the caller supplied and only fails on generated ones.

    At that point **there is no way to tell whether the code is wrong or the case is**,
    so it must not be reported as "the code has a bug" — condemning correct code with a
    wrong case is far harder to debug than missing a bug.
    """
    # this generated case has the wrong expected value: with ties sharing a rank, both
    # records on 3 should be rank 1
    wrong = fence([{"input": {"records": [{"name": "p", "score": 3},
                                          {"name": "q", "score": 3}]},
                    "output": [{"name": "p", "rank": 1}, {"name": "q", "rank": 2}],
                    "note": "a tie (this expectation is wrong)"}])
    client = ScriptedClient([wrong] + [GOOD_CODE] * 3)

    r = compile_function(REQ, SEEDS, client=client, registry=Registry(tmp_path / "r"),
                         sandbox=sb, gen_tests=1)

    assert not r.ok
    assert "passed **every case you supplied**" in r.reason
    assert "Your call" in r.reason and "gen_tests=0" in r.reason


def test_a_failure_on_an_assumed_case_says_the_requirement_is_the_problem(tmp_path, sb):
    """When the failure is on a case that rests on a decision the requirement never
    made, "who is wrong" does not apply — the requirement is underspecified. The report
    has to say that, rather than leaving someone to agonise over code versus case."""
    wrong = fence([{"input": {"records": [{"name": "p", "score": 3},
                                          {"name": "q", "score": 3}]},
                    "output": [{"name": "p", "rank": 1}, {"name": "q", "rank": 2}],
                    "note": "a tie",
                    "assumes": "ties get distinct ranks in input order, no shared rank"}])
    client = ScriptedClient([wrong] + [GOOD_CODE] * 3)

    r = compile_function(REQ, SEEDS, client=client, registry=Registry(tmp_path / "r"),
                         sandbox=sb, gen_tests=1)

    assert not r.ok
    assert "ties get distinct ranks in input order" in r.reason
    assert "requirement is underspecified" in r.reason


def test_failing_on_a_caller_case_is_still_plainly_the_code_s_fault(tmp_path, sb):
    """A failure on a caller's case is plainly the code's fault, and must not get
    diluted by the "maybe the case is wrong" language."""
    bad_code = '```python\ndef solve(params, ctx):\n    return []\n```\n'
    client = ScriptedClient([fence([])] + [bad_code] * 3)

    r = compile_function(REQ, SEEDS, client=client, registry=Registry(tmp_path / "r"),
                         sandbox=sb, gen_tests=1)

    assert not r.ok
    assert "passed **every case you supplied**" not in r.reason
    assert "None of the 3 attempts passed" in r.reason


# --- end to end ----------------------------------------------------------------
def test_generated_cases_land_in_the_test_set_and_are_marked(tmp_path, sb):
    extra = fence([{"input": {"records": [{"name": "p", "score": 3},
                                          {"name": "q", "score": 3}]},
                    "output": [{"name": "p", "rank": 1}, {"name": "q", "rank": 1}],
                    "note": "a tie"}])
    reg = Registry(tmp_path / "r")
    r = compile_function(REQ, SEEDS, client=ScriptedClient([extra, GOOD_CODE]),
                         registry=reg, sandbox=sb, name="rank", gen_tests=1)
    assert r.ok, r.render()

    origins = [e.origin for e in reg.get("rank").tests.examples]
    assert origins == ["caller", "caller", "generated"], \
        ("generated cases belong in the test set, with their provenance kept — next "
         "time a better model regenerates this, they are part of what it is accepted "
         "against")


def test_gen_tests_zero_skips_the_extra_call(tmp_path, sb):
    client = ScriptedClient([GOOD_CODE])
    r = compile_function(REQ, SEEDS, client=client, registry=Registry(tmp_path / "r"),
                         sandbox=sb, gen_tests=0)
    assert r.ok and len(client.calls) == 1 and r.proposal is None


def test_tokens_include_the_test_writing_call(tmp_path, sb):
    r = compile_function(REQ, SEEDS, client=ScriptedClient([fence([]), GOOD_CODE]),
                         registry=Registry(tmp_path / "r"), sandbox=sb, gen_tests=1)
    assert r.ok
    assert r.tokens[0] > r.synth.input_tokens, \
        "the tokens for the test-writing call have to be counted too"
