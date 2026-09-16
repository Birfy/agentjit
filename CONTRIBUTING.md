# Contributing

```bash
pip install -e ".[dev]"
pytest              # 102 unit tests — no network, no tokens
jitagent selftest   # 6 corpus cases, each with a deliberately planted bug
```

Both must pass before anything else is worth discussing. Neither touches the network: the
synthesis loop is driven by `ScriptedClient`, and every corpus case carries its own
implementation.

## The one rule that matters

**A gate that fires on correct code costs more than a gate that misses a bug.** Nothing in
a false positive points at the real cause — it took reading two nearly identical
implementations side by side to find that `datetime.strptime` cannot work inside the
sandbox, and that cost a wasted round of synthesis every time it happened.

So the corpus checks both directions. `01_correct` exists to make sure a correct
implementation sails through; a change that makes it red has failed, however many bugs it
catches elsewhere.

## Adding a verification gate

Seven gates were built here and five were removed, because **not one of them ever caught a
mistake a real model made** — everything they caught was a bug planted by hand. They had
been built by reasoning from a document rather than forced into existence by a real
failure. The reasoning is preserved in [docs/correctness.md](docs/correctness.md); it is
what a new gate has to beat.

A new gate needs:

1. **A real failure it would have caught** — a model output, not a hypothetical.
2. **A corpus case** in `src/jitagent/corpus/`, whose `case.json` names the gate that must
   reject it and why.
3. **Evidence it does not fire on correct code.** `jitagent selftest` covers the corpus;
   for anything subtle, `tools/audit_e2e.py` runs real compiles against independent
   reference implementations.

"Is your test set strong enough?" is **advice to the caller, not a verdict.** A gate that
answers that question belongs in a report, not in the pass/fail path.

## Changing a prompt

`prompts.py` has one rule at the top: **only promise what is actually checked.** The prompt
once threatened the model with a branch-coverage threshold that had already been deleted.
If you change what can reject an implementation, change the prompt in the same commit.

Prompt changes are not covered by the unit tests — they are measured. `tools/audit_e2e.py`
is the measurement, and it costs real tokens.

## The audits

```bash
python tools/audit_tests.py     # are the generated expectations right?  (spends tokens)
python tools/audit_traps.py     # did the generated cases go after the hard parts?  (free)
python tools/audit_e2e.py       # is the compiled function right?  (spends tokens)
```

The protocol in `tools/audit_tests.py` matters more than the numbers: **the reference
implementation has to be written before any generated case is seen.** Write it afterwards
and you start finding reasons why the model's answer was fine, and the audit is worthless.

`audit_traps.py` is why the agreement rate means anything. A model that only ever writes
`f([]) == []` scores 100% and has tested nothing, so each trap is checked as a predicate
over the generated case's *input* — a note claiming to test a tie cannot pass without a tie
in the data.

## The demo in the README

Three steps, and the middle one is yours:

```bash
tools/capture_demo.sh              # runs the CLI for real, writes docs/demo.raw.txt
$EDITOR docs/demo.txt              # pick the lines, trim to <= 92 columns
python tools/make_demo_svg.py      # docs/demo.txt -> docs/demo.svg
```

Everything the animation shows is real captured output. A hand-written demo shows what
someone hoped the tool would do.

## Style

Match the file you are editing. Comments here carry the *reasoning* — why a thing is the
way it is, what was tried and rejected, what would make it wrong — because the mechanisms
are easy to read and the trade-offs behind them are not. A comment that restates the code
is noise; one that records a measurement or a discarded alternative is the point.

## Releasing

Maintainers only. Bump `version` in `pyproject.toml`, add a `CHANGELOG.md` entry, then
publish a GitHub Release tagged `vX.Y.Z`. The release workflow checks the tag against the
version, re-runs the tests, builds, and publishes to PyPI over Trusted Publishing — there
is no API token stored anywhere. `workflow_dispatch` publishes to TestPyPI for a rehearsal.
