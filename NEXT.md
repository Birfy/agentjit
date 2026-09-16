# What to do next

In priority order. Each item says **why**, **how**, and **what counts as done** — an item
with no completion criterion should not be started.

For the current state see the [README](README.md). In one line: **a sentence → generate the
cases → write the code → verify → fetch it back by name.**

---

## 0. A different model, and requirements written by someone else

Every number so far rests on one sample: **Claude Haiku 4.5, with requirements written by
the person running the audit**.

What has been measured (`tools/audit_tests.py`, `tools/audit_traps.py`,
`tools/audit_e2e.py` — all re-runnable):

- 6 requirements × 8 generated cases, expectations **48/48 in agreement** with the
  reference, and **18/18 of the planted traps** exercised by at least one case
- 6 requirements compiled, the result agreeing with the reference on **every random input**

Those numbers look good, but **the audit's oracle and the requirements come from the same
person**. Someone writing a requirement while knowing what they intend to test writes more
clearly than they realise, and their reference implementation shares their reading. Those
two together are a systematic bias.

How:

- **Change the model**: run the same 6 requirements through Sonnet and Opus, one round
  each, and see whether the generated expectations still agree 48/48. **The more valuable
  variant is a cross-check**: verify model B's code with model A's cases, because every
  disagreement is a place the requirement failed to say something.
- **Change the author**: find 5 requirements **written by someone else** (lifted from a
  real project) and re-run both audits.
- **Quantify vague requirements**: there is only a qualitative result today (the model does
  declare its assumptions). What is wanted is a number — N vague requirements × M unsaid
  decisions, how many were declared, how many missed, how accurate the declarations were.

**Done when**: there is a number that still holds up under a different model and a
different author — or a clear conclusion that it does not.

---

## 1. The scenarios are still narrow

Up from 4 tasks to 10 (group and sum, tiered fees, counting rows, ranking, log parsing,
counting working days, flattening nested data, top N per group, stateful aggregation,
splitting an amount) — but every one is a small pure data transformation, and every one
fits in under 20 lines.

Shapes never touched: anything needing helper functions, anything recursive, anything where
a thousand input rows make complexity matter, anything whose output shape bears no
resemblance to its input.

**Done when**: 10 or more requirements where the implementation is not obvious at a glance
have been run, with the success rate and the failing gate recorded.

---

## 2. Numbers that need calibrating

Not many left; after the cull there are four:

| Parameter | Current | The question |
| --- | --- | --- |
| `gen_tests` | 8 | how many cases to write. More costs tokens and raises the chance of a wrong one; fewer leaves the criteria thin |
| `max_attempts` | 3 | across 15 rounds only one requirement ever reached attempt 2 or 3 — and the root cause was a sandbox limitation, since fixed. A case that genuinely needs several rounds has not been seen |
| `lookup.MIN_SIMILARITY` | 0.20 | **script-dependent, so it cannot separate anything on its own.** Chinese: unrelated ~0.03, genuine rewrites 0.29-0.41. English: unrelated 0.21-0.28 (the bigram floor alone), rewrites 0.51-0.54. Either normalise against a per-language baseline, or accept it as a pure cost knob and say so |
| `llm.CLI_OVERHEAD_TOKENS` | 22200 | the fixed overhead `claude -p` carries on every call, measured against an empty task. It moves with the CLI version |

---

## 3. Explicitly not doing

- **Putting the cut gates back** — not unless a real failure demands it. The hold-out split
  is the only one that would go back voluntarily, and the trigger for that is item 0 finding
  the model writing answers against the cases it can see
- **A container or microVM sandbox** — the current subprocess sandbox is a **correctness
  sandbox, not a security sandbox**, which is enough while it is running your own code. It
  has to be upgraded before it touches an untrusted source
- **Capability injection (http / fs / tools)** — pure functions cover more ground than
  intuition suggests; get pure functions right first
- **Vector retrieval** — the whole registry is scanned linearly today; revisit when it
  becomes too big to scan. It is a **performance** optimisation, not a correctness
  mechanism, and whoever swaps it in must not "optimise away" the re-verification with it

---

## Read before starting

1. The header of `src/agentjit/propose.py` — one model writing both the code and the tests
   that judge it is circular; which part of that each of the three mitigations solves, and
   which part cannot be solved
2. The header of `src/agentjit/verify.py` — which five gates were cut, why, and which one is
   most likely to come back
3. The header of `tools/audit_tests.py` — the audit protocol. The reference implementation
   has to be written **before** any generated case is seen, or it starts finding reasons why
   the model's answer was fine
4. The header of `src/agentjit/lookup.py` — why retrieval is allowed to be crude, and where
   being crude does not matter
