"""Tests for the synthesis loop — scripted client throughout: no network, no tokens,
deterministic results.

What is under test is the behaviour of **the loop itself**: does the feedback carry the
specific failure, does it stop after three attempts, and does a reply with no code block
get fed back.
"""
import pytest

from jitagent import Example, Sandbox, Thresholds
from jitagent.llm import ScriptedClient
from jitagent.synth import compile_function

REQ = ("Group rows by type and sum the amounts, returning {type: total}. "
       "An amount may carry a currency symbol and thousands separators; "
       "an empty one counts as 0.")

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
    return f"Clean each value first, then accumulate.\n\n```python\n{body}\n```\n"


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


# --- the repair loop --------------------------------------------------------------
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

    # the second call has to carry the specific static-check violation, not "try again"
    second = client.calls[1]["user"]
    assert "no import" in second and "<previous_attempt>" in second

    # the third has to carry which example, what was expected, and what came back
    third = client.calls[2]["user"]
    assert "expected" in third and "actual" in third
    assert "1200.5" in third                       # the actual value, not a vague gesture


def test_gives_up_after_max_attempts(sb):
    r, client = run([NO_CLEAN, NO_CLEAN, NO_CLEAN], sb)
    assert not r.ok and len(client.calls) == 3
    assert "None of the 3 attempts passed" in r.reason
    assert "examples" in r.reason


def test_missing_code_block_is_fed_back(sb):
    r, client = run(["I suggest you write this function yourself.", CORRECT], sb)
    assert r.ok
    assert r.attempts[0].gate == "no_code"
    assert "no ```python block" in client.calls[1]["user"]


# --- schema inference -----------------------------------------------------------
def test_schemas_are_inferred_from_examples(sb):
    r, _ = run([CORRECT], sb)
    assert r.spec.param_schema["properties"]["rows"]["type"] == "array"
    # the return keys differ every time — infer a mapping, do not pin properties to
    # the keys that happened to be seen
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
