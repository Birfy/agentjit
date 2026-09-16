<div align="center">

# agentjit

**A paragraph of text goes in. Code comes out. You fetch it back by name.**

[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776ab?logo=python&logoColor=white)](https://www.python.org/downloads/)
[![tests](https://img.shields.io/badge/tests-102%20passing-5ac489)](tests/)
[![corpus](https://img.shields.io/badge/corpus-6%2F6-5ac489)](tests/corpus/)
[![no API key needed](https://img.shields.io/badge/API%20key-not%20required-a78bfa)](#backends)
[![status](https://img.shields.io/badge/status-working%20prototype-e0af68)](#known-gaps)

<img src="docs/demo.svg" alt="agentjit: compile a requirement, call it, list the registry" width="100%">

</div>

An agent is good at working out *what* to do. It is expensive and unreliable at doing the
same thing two hundred times. `agentjit` takes the repetitive part, compiles it into a
real function once, verifies it, and stores it under a name you choose.

This is what a JIT does to an interpreter: a hot path should not be re-derived on every
pass. Compile it once, then just run it.

```
first call    synthesise + verify   ~20-60s,  a few thousand tokens
every call    sandboxed execution   ~30ms,    zero tokens
```

---

## Quickstart

```bash
git clone https://github.com/Birfy/agentjit && cd agentjit
pip install -e ".[dev]"
```

You need **no API key**. If you have [Claude Code](https://claude.ai/code) installed,
`agentjit` shells out to it and uses its authorisation.

Check everything works — no network, no tokens:

```bash
pytest              # 102 unit tests
agentjit selftest   # 6 corpus cases, each with a deliberately planted bug
```

### Compile something

A requirement is a JSON file: a sentence, plus a few examples that pin down what you mean.
The examples are not decoration — they are the spec, and nothing without them reaches the
cache.

```json
{
  "requirement": "Rank {name, score} records from highest score to lowest. Records with the same score share a rank, and the next rank skips accordingly (1, 1, 3 — not 1, 1, 2). Within a tie, order by name ascending.",
  "examples": [
    { "input":  { "records": [{"name": "alice", "score": 90},
                              {"name": "bob",   "score": 85},
                              {"name": "carol", "score": 90}] },
      "output": [{"name": "alice", "rank": 1},
                 {"name": "carol", "rank": 1},
                 {"name": "bob",   "rank": 3}],
      "note": "tie, then skip" },

    { "input": { "records": [] }, "output": [], "boundary": true }
  ]
}
```

That is [`examples/rank.json`](examples/rank.json), so you can run the next block as it
stands.

```bash
agentjit compile examples/rank.json --name rank   # write the cases, then the code
agentjit get     rank                             # print the source
agentjit call    rank '{"records": [...]}'        # run it in the sandbox
agentjit list                                     # what is in the registry
agentjit search  "rank records by score"          # is there one already?
agentjit inspect rank                             # cases, versions, verification report
```

The registry lives in `~/.agentjit/registry` — set `AGENTJIT_HOME` or pass `--home` to put
it somewhere else.

### From Python

```python
from agentjit import compile_function, get_code, call_function, Example
from agentjit.llm import ClaudeCliClient

compile_function(
    "Rank {name, score} records from highest score to lowest...",
    [Example({"records": [{"name": "alice", "score": 90}]}, [{"name": "alice", "rank": 1}]),
     Example({"records": []}, [], boundary=True)],
    name="rank",
    client=ClaudeCliClient(),
)

get_code("rank")                           # the source, as a string
call_function("rank", {"records": [...]})  # run it, 200 times if you like
```

The CLI picks a backend for you; the library does not. `compile_function` raises
`NeedsClient` on a cache miss with no `client=`, rather than quietly shelling out to
something — reading the cache and spending tokens should not look the same from the
outside.

### Backends

`compile_function` takes any object with `complete(system=, user=)`. Three come with it:

| Backend | Needs | Use |
| --- | --- | --- |
| `ClaudeCliClient` | the local `claude` CLI | **the default**; no API key |
| `AnthropicClient` | `ANTHROPIC_API_KEY` | direct API access |
| `ScriptedClient` | nothing | replays canned replies; how the tests run with no network |

Swapping in your own model is one class with one method.

---

## How a compile works

```
a sentence + a few seed examples
   │
   │  call 1  write the test cases   ← the code does not exist yet, so it cannot
   │                                   influence the cases
   │  call 2..4  write the code → static check → run every case
   │             → structured feedback → write it again
   ▼
stored: the cases + the code + the verification report
```

**Generating the cases before the code is part of the design, not an implementation
detail.** The obvious objection to any of this is circular reasoning: one model writing
both the code and the tests that judge it. Writing the cases first removes the strongest
link in that circle — when the cases are written, there is no implementation for them to
be shaped by.

The link it cannot remove is "the same model misreads the requirement the same way twice".
So, three more things:

- **Your seed examples are the anchor.** A generated case that collides with a seed is
  dropped, and the drop is reported.
- **Generated cases are marked** `origin="generated"`. A failure that only occurs on
  generated cases is **ambiguous** — the code may be wrong, or that case's expected value
  may be. `agentjit` reports it for you to adjudicate rather than calling it a bug.
  Condemning correct code with a wrong case is far harder to debug than missing a bug.
- **Anything the requirement left open must be declared.** If a case rests on a decision
  the requirement never made — which way `0.5` rounds, whether an empty string counts as
  empty — the model has to record it in an `assumes` field. That field exists because
  measurement forced it; see [below](#vague-requirements-are-the-real-risk).

In short: generating cases makes the criteria **thicker**, not more **trustworthy**. Only
your examples do the second thing.

Three things can fail a synthesis, and no more: the static check, one of the cases, or a
return value that breaks the schema inferred from your examples — and that last one is not
a separate opinion, it is the same check the runtime applies on every call. The feedback is
structured — `input X / expected Y / actual Z / traceback` — never "that failed, try
again".

---

## Four design claims

- **Correctness rests on test cases, not on extra machinery.** There used to be five more
  gates here — a hold-out split, branch coverage, fuzzing, mutation testing, post-assertions
  — all answering "are your cases strong enough?". They are gone. That is **advice to the
  caller, not a verdict**, and across the whole corpus none of the seven gates ever caught a
  mistake a real model made.
- **`agentjit` writes the cases out in full; the trust still comes only from you.** See
  above.
- **No holes in the sandbox.** Generated code has no I/O capability at all. Anything
  external has to be requested from the host over IPC, which authorises, meters and audits
  it. Capability control lives in one place.
- **The cases are the asset; the code is regenerable.** The registry is organised around
  the test set, and the code is just an implementation that currently passes it. A better
  model means a free regeneration of everything.

---

## Does it actually work?

Everything below is measured and re-runnable. The audits share one oracle: six
**reference implementations, transcribed literally from the requirement text and written
before any generated case or any generated code existed**. Six requirements, deliberately
spread across shapes — log parsing, counting working days, flattening nested data, top N
per group, stateful aggregation, splitting an amount with rounding.

### Audit 1 — are the generated cases right?

```bash
python tools/audit_tests.py && python tools/audit_traps.py
```

| | |
| --- | --- |
| generated expectations agreeing with the reference | **48 / 48** |
| planted traps exercised by at least one case | **18 / 18** |

The first number alone would mean nothing: a model that only ever writes `f([]) == []`
scores 100% and has tested nothing. **The second is what makes the first worth reading.**
The traps are the places each requirement is easy to misread — a `]` inside the message
body, `start` later than `end`, an empty dict that has to be discarded, two people on the
same salary, a duplicate `start`, a remainder that has to be handed out by descending
weight — and `audit_traps.py` checks each one **as a predicate over the case's input**, so
a note claiming to test a tie cannot pass without a tie in the data.

### Audit 2 — is the compiled function right?

```bash
python tools/audit_e2e.py
```

Audit 1 checks the cases. This checks the final product, against the same references, on
200 random inputs each.

<!-- E2E NUMBERS -->

Both directions have to be visible: **passing its own cases but failing here** means the
generated cases were too weak and missed a real bug; **failing its own cases but passing
here** means a generated case had a wrong expectation and condemned correct code.

### Vague requirements are the real risk

The requirements above were all written by one person and **written precisely on
purpose**. Real requirements are vague, and vagueness is where the risk is. A separate
round on deliberately vague ones ("round to the nearest integer" without saying which way
`0.5` goes, "deduplicate" without saying on what, "strip empty values" without saying
whether an empty string counts) split two ways:

- **Dodging it** — writing `1.5` but not `2.5` (both readings agree on `1.5`; only `2.5`
  forks); testing `null` but not `""` or `0` or `false`. Safe, but it resolves nothing.
- **Asserting silently** — on "deduplicate a list of records", three decisions the
  requirement never made ("compare whole records", "keep the first", "preserve order")
  were written into the cases as settled fact. **This is the dangerous one**: a caller who
  read it the other way gets their correct implementation condemned.

That is why the `assumes` field exists: a decision the requirement did not make, that the
model settled itself, **has to be written down**. Re-running the same three requirements
afterwards, the behaviour inverted — `2.5` was written (declaring "rounds away from zero")
and `{a:0, b:false, c:"", d:null}` was written (declaring "empty means null only"). When a
failure lands on such a case, the report says plainly that **this is not anyone being
wrong, it is the requirement being underspecified.**

> That specific vague-requirement experiment was run before the translation, on the
> Chinese prompts, and has not been repeated word for word. What the English run does show
> is that the mechanism is live: **4 of the 6 requirements above produced at least one
> declared assumption** (5 cases in total), unprompted, on requirements that were written
> to be precise. `tests/test_propose.py` pins the attribution behaviour that follows.

---

## Reuse: fetch it back by name

Compiling once saves nothing. The saving is in not compiling the second time.

What is stored is the **test set**; `code.py` is just an implementation that currently
passes it. One `spec_hash` can hold several versions, and the newest wins. The cases belong
to the spec rather than to a version — otherwise every regeneration copies them, the copies
drift, and "a better model means a free regeneration" loses the thing it rests on.

A call goes through three steps: **param schema → sandbox → return schema.** Both schemas
are inferred from the shape of your examples, not guessed, and they are the same yardstick
verification used. **A call is a pure read** — nothing is written to disk, there is nothing
to flush, and concurrent processes do not fight.

### Lookup: retrieval affects the hit rate, re-verification decides correctness

There are a thousand ways to phrase the same requirement, so using the text as a cache key
gives a hit rate close to useless. Lookup has three levels:
**L1 exact `spec_hash` → L2 candidate retrieval + re-run *this run's* examples → L3 miss,
go synthesise.**

One measurement shaped the whole design. Take a requirement — "group CSV rows by the type
field and sum the amount" — and compare its character-bigram overlap with three genuine
rewrites, and with one sentence that changes only "sum" to "average". The comparison is
pinned by a test, in `tests/test_lookup.py`:

| Compared against | Similarity |
| --- | --- |
| three genuine rewrites | 0.54 / 0.52 / 0.51 |
| **"sum" changed to "average"** | **0.61** |
| an unrelated requirement (ranking) | 0.26 |

**The one that behaves completely differently is closer, as text, than any honest
rewrite.** [design.md §6.1](docs/design.md) argues that embeddings put them just as close
together, so this is not something switching to vectors fixes — that part is an argument,
not a measurement, but the conclusion below does not depend on it.

The conclusion: **retrieval quality affects the hit rate, not correctness.** An L2 hit
*must* re-run the requesting caller's examples; failing that, it is treated as a miss. So
retrieval is allowed to be crude — crude retrieval misses a few hits, it can never hand
back a wrong function. A vector index is an optimisation for when the registry outgrows a
linear scan, **not** a correctness mechanism. Keep those two apart and L2 stops being
scary.

The schema filter validates **this run's examples against the candidate's schema**, rather
than comparing two schemas structurally. The latter needs a definition of "compatible";
the former asks the question that actually matters, and uses the same yardstick as the
runtime input guard — so you can never get the contradiction where lookup says compatible
and the guard then rejects the call.

**An L1 hit is re-verified too**, which the design document said was unnecessary. It costs
one sandbox run and no tokens, and it catches a real situation: same requirement text, but
this time your examples differ from last time — because the old expectation was wrong, or
because the requirement has been re-understood. Serving the stored version then means
shipping an implementation already known not to satisfy the current criteria. When
re-verification fails, a new version is synthesised: `cache: reused_with_new_version`.

After a hit, **this run's examples are merged into the test set**. They just passed
re-verification, so they are real criteria the current implementation agrees with — a cache
hit thickens the function on its way past, and the next regeneration is that much safer.

---

## What was cut, and why

The decision rested on one fact: **of seven gates, not one ever caught a mistake a real
model made.** Everything they caught was a bug planted by hand in the corpus. So they were
all built by reasoning from a document, not forced into existence by a real failure.

Removed: hold-out splitting and rotation, the 100% branch-coverage threshold, fuzzing,
determinism checking, mutation testing, post-assertions, probes, `QUARANTINED`
sequestration, three-way version ranking, and `net_savings` accounting.

| | before | after |
| --- | --- | --- |
| lines under `src/` | 3647 | 2450 |
| one verification run | 727ms | 34ms |
| tunable parameters | 21 | 4 |

(`src/` measures 2914 lines today; the translation to English added prose, not machinery.)

**The most likely thing to come back is the hold-out split**: keeping some cases away from
the repair loop, which guards against a model writing `if input == X: return Y` against the
cases it can see. It costs no extra tokens. The reasoning is recorded at the top of
`verify.py`.

---

## The corpus: a gate that fires on the wrong thing is also a failure

```bash
agentjit selftest
```

```
ok   01_correct         VERIFIED   -          a correct implementation should sail through
ok   02_wrong_no_clean  REJECTED   examples   no cleaning, so it fails the very first case
ok   08_malicious       REJECTED   static     reads a file and escapes via getattr
ok   09_insufficient    EPHEMERAL  -          no cases, so nothing to judge — not cached
ok   10_timeout         REJECTED   examples   a loop that never exits; the wall clock catches it
ok   11_memory_bomb     REJECTED   examples   a memory bomb; rlimit or the watchdog contains it
```

**Both a miss and a false positive count as failure.** A gate that fires on correct code is
harder to diagnose than one that lets a bug through — which is exactly what the cull above
was about.

---

## Known gaps

- **One model, three attempts maximum.** Every number here rests on Claude Haiku 4.5. What
  happens with a different model, a different temperature, or more rounds is unknown.
- **Vague requirements have only a qualitative result.** After the `assumes` change the
  model does declare its assumptions, but how *accurate* those declarations are, and how
  often it misses one it should have made, has not been quantified.
- **Every requirement here was written by the same person who wrote the audit.** Someone
  writing a requirement while knowing what they intend to test writes more clearly than
  they realise.
- **The reference implementations share one reading with their author.** The audit oracle
  is a literal transcription of the requirement — so if the model misreads the requirement
  the same way the author did, this audit cannot see it. The requirements are the author's,
  so "my reading is the spec" holds here; it would not for someone else's requirement.
- **The sandbox is a correctness sandbox, not a security sandbox.** Restricted builtins, an
  AST allowlist, rlimits and a memory watchdog contain accidents and casual escapes. They do
  not contain a serious attacker. macOS ignores `RLIMIT_AS` outright, so the memory ceiling
  there rests on the parent polling RSS.
- **`datetime.strptime` does not work** inside the sandbox — it imports `_strptime` on first
  call, and the restricted builtins have no `__import__`. Other lazily-importing standard
  library functions may have the same problem; this is the only one hit so far. The prompt
  tells the model to use `datetime.date.fromisoformat` instead, and a test keeps the
  limitation and the prompt in step.
- **The lexical similarity floor is script-dependent.** In Chinese two unrelated
  requirements score ~0.03; in English the shared-bigram floor alone puts them at ~0.21-0.28,
  against `MIN_SIMILARITY = 0.20`. No single number separates both, which is why the
  threshold is only a cost knob — the schema check keeps an unrelated candidate out of the
  sandbox, and re-verification is what keeps a wrong one out of your hands.
- **Token counts through the CLI are estimates.** `claude -p` carries about 22.2k tokens of
  fixed overhead (Claude Code's own system prompt and tool definitions). It is subtracted,
  but the constant moves with the CLI version.

---

## The code

```
src/agentjit/
  jit.py            the product surface: compile / get_code / call / search / inspect
  propose.py        get the model to write the cases out — before the code, separate call
  prompts.py        the two prompts; the requirement sits in an untrusted data region
  synth.py          the synthesis loop: write → verify → structured feedback → write again
  verify.py         the static check plus running the cases. Those two, and no more
  static_check.py   the AST allowlist — the cheapest gate; failing it means no sandbox
  sandbox.py        subprocess execution plus the parent-side memory watchdog
  _child.py         the sandbox child; must be self-contained
  infer.py          infer a schema from examples (it can tell a record from a mapping)
  registry.py       storage: organised around the test set, versioned, fetched by name
  lookup.py         three-level lookup — retrieval narrows, re-verification decides
  runtime.py        calling: input guard → sandbox → return guard. A pure read
  llm.py            backends: the API, the local claude CLI, a scripted replay
tests/corpus/       6 corpus cases, one per gate
tools/              the two audits, plus the demo capture and the SVG builder
examples/           two demo requirements for `agentjit compile`
```

| Document | Contents |
| --- | --- |
| [docs/design.md](docs/design.md) | the main design (v0.2) — interface, architecture, caching, sandbox, roadmap |
| [docs/correctness.md](docs/correctness.md) | correctness and testing — **what decides whether this stands up** |
| [docs/tracing-frontend.md](docs/tracing-frontend.md) | the front end that spots repetition and triggers a compile (M4, not started) |
| [NEXT.md](NEXT.md) | what to do next, and what is deliberately not being done |

### Where to start reading

1. `src/agentjit/propose.py`, the header — one model writing both the code and its tests is
   circular; which part of that each mitigation solves, and which part cannot be solved
2. `src/agentjit/verify.py`, the header — which five gates were cut, why, and which one is
   most likely to come back
3. `tools/audit_tests.py`, the header — the audit protocol; the reference has to be written
   **before** any generated case is seen, or it starts making excuses for the model
4. `src/agentjit/lookup.py`, the header — why retrieval is allowed to be crude, and where
   being crude does not matter
