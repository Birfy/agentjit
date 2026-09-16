# Correctness and testing

| Field | Value |
| --- | --- |
| Status | Draft |
| Version | 0.1 |
| Relation | Expands [design.md](design.md) §6.2 / §8; this is what decides whether the system stands up at all |

> **What is actually built.** This document is the design argument, not a description of
> the shipped code. Of the five oracle tiers below, only **T1 (caller examples)** is
> implemented, and of the strength checks in §8–§9 none are: hold-out splitting, branch
> coverage, fuzzing and mutation testing were all built and then removed, because across
> the whole corpus not one of them ever caught a mistake a real model made. See the
> "What was cut, and why" section of the [README](../README.md) and the header of
> `src/jitagent/verify.py`. The reasoning here is kept because it is what any future
> addition has to argue against.

---

## 1. The fundamental difficulty: circular reasoning

Whether generated code is correct can only be judged by test cases. But where do the
cases come from?

If the cases come from the same model, the reasoning goes in a circle: **the model
misreads the requirement → the code it writes and the cases it writes rest on the same
misreading → everything passes → a self-consistent wrong answer is delivered.** And that
is the hardest kind of error to find, because nothing crashes and nothing raises; it just
quietly computes the wrong thing.

**You cannot bootstrap correctness out of a model's own understanding.** Any scheme where
an LLM writes tests to verify LLM-written code adds no information at all, as long as the
two share one reading of the requirement.

So the core question is: **where do you get a criterion that does not depend on the
model's understanding?**

There are five sources, differing in reliability and cost. They are not alternatives;
they are gates that **stack**.

---

## 2. Oracle tiers

| Tier | Oracle | Needs an answer key? | Cost | What it catches | When to use |
| --- | --- | --- | --- | --- | --- |
| **T1** caller examples | I/O pairs from a person or an agent | yes | 0 (the caller supplies them) | semantic errors; the strongest | whenever there are any |
| **T2** metamorphic properties | invariants derived from the requirement | **no** | low | a large class of semantic errors | always |
| **T3** differential testing | N independent implementations compared | **no** | medium | **locating ambiguity** | when the expected call volume is high |
| **T4** shadow execution | the LLM slow path as a reference | **no** | high | semantic errors on the real distribution | high-value functions |
| **T5** property / fuzz | random inputs from the schema | no | very low | crashes, schema violations, boundaries | always |

The key judgement: **T2 and T3 are badly underrated — they catch semantic errors without
needing an answer key.** The design should not be staked on T1.

---

## 3. T1 — caller examples

The strongest, and the scarcest. The rule is in [design.md §6.2](design.md): nothing
reaches the persistent cache without examples.

The point to remember: **do not expect the caller to write many.** Two or three is the
ceiling. So T1's job is to anchor the semantics, not to cover the input space — coverage
is T2's and T5's job.

---

## 4. T2 — metamorphic properties: one property is worth ten thousand cases

This is the most important section in this document.

**A metamorphic relation is a constraint you can check without knowing the right answer.**
You do not know what `f(x)` should be, but you do know that `f(shuffle(x))` must equal
`f(x)`. That sentence can be checked against **any generated input**.

A user who gives 3 examples has verified 3 points in the input space. A user who confirms
3 properties has verified the whole space. **That is several orders of magnitude, and the
cognitive load on the caller is lower, not higher — judging "shuffling the rows shouldn't
change the result, right?" is much easier than constructing an input/output pair.**

### 4.1 The common properties in this domain

The target scenarios here — parsing, cleaning, transforming, aggregating, formatting,
validating — happen to be exactly where metamorphic properties are densest:

| Property | Form | Applies to |
| --- | --- | --- |
| **permutation invariance** | `f(shuffle(xs)) == f(xs)` | aggregation, sums, grouping, statistics |
| **conservation** | `sum(f(xs).values()) == sum(amounts(xs))` | group-and-sum, splitting, routing |
| **homomorphism / additivity** | `f(A ++ B) == merge(f(A), f(B))` | grouped aggregation, counting, unions |
| **idempotence** | `f(f(x)) == f(x)` | cleaning, normalisation, formatting, deduplication |
| **monotonicity** | more `xs` ⇒ the value for a given key never decreases | counting, accumulation |
| **empty element** | `f([]) == the empty result` | almost everything |
| **range closure** | `keys(f(xs)) ⊆ types(xs)` | grouping, mapping, classification |
| **round trip** | `decode(encode(x)) == x` | serialisation, codecs, conversion |
| **size relation** | `len(filter(xs)) <= len(xs)` | filtering, deduplication |

### 4.2 Where properties come from

Three routes, in order of reliability:

1. **The caller states them** — the most reliable, but it takes initiative and almost
   nobody does it
2. **The model proposes, the caller confirms** — **the main path**. The model derives
   candidate properties from the requirement and the caller confirms or rejects each one.
   Confirming a property is a yes/no judgement, an order of magnitude cheaper than
   writing a test case
3. **Template matching** — infer from the requirement's intent using the table above
   ("group and sum" → automatically brings permutation invariance, conservation and the
   empty element). **Only counted towards the verification level once the caller has
   confirmed it**; unconfirmed properties are demoted to advisory checks, recorded and
   surfaced on failure but not blocking

**Never treat an unconfirmed property as a hard gate.** A property the model proposes can
itself be wrong (it might propose "the sort is stable" when the requirement does not care
about order), and using a wrong property to reject correct code makes the system stick in
exactly the places where it was right — a failure that is harder to debug than letting a
bug through.

### 4.3 How properties get checked

For each confirmed property, generate N random inputs from the schema (200 by default)
and check it on each. The generator should lean towards boundaries: empty collections,
single elements, duplicate keys, zeros, negatives, very long strings, special characters,
extreme numbers.

On failure, report a **minimised counterexample** (delta debugging down to the smallest
failing input). "Conservation fails on `[{type:"a",amount:"-0"}]`" is far more useful than
"it fails on this 200-line random input", both for the model repairing it and for a person
debugging it.

---

## 5. T3 — differential testing: really an ambiguity locator

Synthesise N implementations independently (different prompt wordings, different
temperatures, possibly different models), run them on generated inputs, and compare.

The naive reading is "majority vote". **But the real value is not in the voting — the
points of disagreement pinpoint exactly what the requirement failed to say.**

```
input:           {rows: [{type: "sale", amount: "-50"}]}
implementation A: {sale: -50.0}      keeps the negative
implementation B: {sale: 0.0}        filters the negative out as bad data
implementation C: raises

→ the requirement never mentioned negative amounts. Each one guessed.
```

That directly answers one of this system's open questions:

> **Do not ask the caller to set the questions. Ask the caller to adjudicate.**

```
Q: "What should happen when an amount is negative?"
   [ keep it ] [ treat as zero ] [ raise ] [ doesn't matter, that data won't occur ]
A: treat as zero
→ generate a test case automatically; it joins this function's regression set permanently
```

For the caller it is a multiple-choice question, a few seconds of work — and what comes
out is a test case that lands **exactly on the sore spot**, covering a place that is
genuinely ambiguous rather than a place the caller imagined. Far higher quality than
questions they set themselves.

**This should be the first choice when T1 examples are thin, not a fallback.**

The cost is N times the synthesis tokens (about 3x at N=3). Entirely worth it for a
function that will be called 200 times; not worth it for one called a handful of times. So
gate it on the expected call volume — `compile_function` can take an `expected_calls` hint.

The agreeing part is not wasted either: N independent implementations agreeing across a
large number of random inputs is a reasonably strong correctness signal (though it does
not rule out a shared misreading).

---

## 6. T4 — shadow execution

For a high-value function in its first period live, run both paths: the compiled path's
result goes back to the caller, while the same input also goes to the LLM slow path, and
the two are compared.

This is the only means of verifying against the **real input distribution** — T2, T3 and
T5 all use generated inputs, and real data has a way of being shaped unexpectedly.

```
agreement >= 98% over >= 20 samples  → promote to CONFIRMED
agreement < 98%                      → feed the disagreeing samples back as counterexamples
```

Expensive (double cost on every call), so only for high-volume, high-impact functions, and
only at effect class `IDEMPOTENT_WRITE` or below.

The comparison cannot be string equality: structured fields compare strictly, free text
compares by semantic similarity, numbers get a tolerance.

---

## 7. T5 — property and fuzz testing

Generate random inputs from the param schema and check only **what needs no semantic
knowledge**:

- it does not crash, time out, or exceed its memory
- the return value matches the return schema
- it calls no unauthorised facade
- determinism: the same input twice gives the same result (catching code that quietly uses
  randomness or the current time)

Very cheap; turn it on without thinking. It catches no semantic errors, but it catches a
large class of robustness problems — and the most common flaw in generated code is exactly
"the happy path is right, and it explodes on a null or an odd format".

---

## 8. Is the test set strong enough?

The above answers "where do the cases come from". There is an equally important second
question: **what does passing actually tell you?**

Three cases passing may mean the code is correct, or it may mean those three cases are
weak. It needs quantifying.

### 8.1 Branch coverage: an uncovered branch is unverified code

Collect coverage while running the tests. **Every branch of the generated code must be
covered, or it does not get `VERIFIED`.**

When a branch is uncovered, the order of treatment matters:

1. **First, get the model to delete it.** Generated code is full of useless defensive
   branches (`if not rows: return {}` where the loop below handles an empty input
   perfectly well). Deleting beats adding a case — **the smaller the code, the smaller the
   surface that needs verifying.**
2. If it cannot be deleted (it is real logic), generate a case for that branch and confirm
   the expected output through the adjudication flow.

This gate is cheap, objective, needs no semantic knowledge, and squeezes the redundant
branches out of the generated code as a side effect.

### 8.2 Mutation testing: an unexpected dividend of the pure-function design

Coverage says the code was executed, not that an error would be caught. Mutation testing
quantifies the latter directly: **break the code on purpose (flip a comparison, change an
arithmetic operator, ±1 a constant, negate a boolean, delete a statement, change a return
value) and see whether the test set catches it.**

A mutant that survives is a blind spot in the test set.

```
mutation_score = mutants killed / valid mutants
VERIFIED threshold: >= 80%
```

Mutation testing is too slow for normal engineering and nobody uses it. **Here it is
cheap**, which is the unexpected dividend of the pure-function design:

```
a 20-line pure function → about 50 mutants
each mutant runs 5 cases × 1ms = 5ms
250ms in total
```

Milliseconds. **Entirely affordable, and it is the only means of quantifying test-set
strength that needs no answer key.** A surviving mutant also tells you directly what case
is missing — "changing the `>` on line 7 to `>=` went unnoticed" is a precise description
of the gap.

---

## 9. Anti-overfitting: the model will write to the test set

The repair loop ([design.md §6.3](design.md)) feeds failing cases back to the model. After
three rounds, the model may well have written code that is **only correct on those cases**
— in the extreme, a literal `if input == X: return Y`.

Three measures:

**1. Hold-out split (hard requirement)**

Split the cases in two:

```
visible to the repair loop:  70% (at least 1)
held out for acceptance:     30% (at least 1), never seen by the repair loop
```

The hold-out runs once, at final acceptance. **A hold-out failure is not "one more repair
round" — that only deepens the overfitting.** The treatment: allow one hold-out rotation
(pick a different subset to hold out and re-run synthesis); a second failure means
synthesis failed.

With fewer than 3 cases in total there is no meaningful hold-out, and in that case T2
properties or T3 differential testing **must** make up the difference, or there is no
`VERIFIED`.

**2. No hard-coded answers (static)**

A static check: the code must not contain the literal output of a test case. This does not
stop clever overfitting, but it stops the dumbest and most common kind.

**3. Tests first (ordering)**

The initial case set must be fixed before the code exists, and the context in which the
cases are generated must contain no code. This weakens — but does not eliminate — the
correlation between code and cases.

Showing the model the failure details inside the repair loop is necessary and does not
violate this — **the point is that the case set itself must not be shaped by the code.**

---

## 10. The test set is the asset; the code is regenerable

This one changes what the registry should store.

A function's core asset **is not its code, it is its test set**. The code can be
regenerated at any time with a better model; the test set is accumulated a piece at a
time, and what accumulates in it is real understanding of the requirement.

Therefore:

- **The registry is organised around the test set.** Code is just "an implementation that
  currently passes it".
- **A better model means a free regeneration of everything.** Re-run synthesis for every
  function with the new model, accept each against its own test set, and swap in whatever
  passes. The thicker the test set, the safer this is.
- **The test set only grows.** It is monotonic:

| Source | When |
| --- | --- |
| caller examples | at the first compile |
| confirmed metamorphic properties | at the first compile |
| adjudicated differential disagreements | during synthesis |
| shadow-execution disagreements | early in production |
| **guard failures from production inputs** | **continuously** |
| errors found by human debugging | continuously |

The last two are where the long-term value is: **every production failure becomes a
permanent regression case.** The older the function, the thicker its test set and the
safer regeneration becomes. That is a positive feedback loop.

---

## 11. Verification level thresholds

| Level | Threshold | Cacheable | Reusable |
| --- | --- | --- | --- |
| `EPHEMERAL` | none | ❌ | ❌ |
| `VERIFIED` | T1 ≥2 examples (≥1 a boundary) **or** ≥2 T3 adjudications<br>+ all of T5<br>+ 100% branch coverage<br>+ mutation score ≥80%<br>+ hold-out passes | ✅ | ✅ |
| `CONFIRMED` | `VERIFIED` + ≥1 confirmed T2 property<br>+ (optional) T4 shadow agreement ≥98% | ✅ | ✅ preferred |
| `QUARANTINED` | was `VERIFIED`, then failed 3 times consecutively at runtime | kept | ❌ |

Note the **"or"** on the `VERIFIED` row: T1 examples and T3 adjudications are
interchangeable entry points. That is the answer to the open question "what if the caller
gives no examples" — **take a different route to a criterion of the same quality, rather
than lowering the bar.**

---

## 12. Cost

Take a 20-line pure function with 5 cases and 3 properties:

| Gate | Cost |
| --- | --- |
| static check | < 10ms |
| T1 cases | 5 × 1ms = 5ms |
| T5 fuzz (200 inputs) | 200ms |
| T2 properties (3 × 200 inputs) | 600ms |
| branch coverage | one run with profiling, ~50ms |
| mutation testing (50 mutants × 5 cases) | 250ms |
| **execution subtotal** | **about 1.1 seconds** |
| T3 differential (optional, 3 implementations) | +2x synthesis tokens |
| T4 shadow (optional) | double cost on every call |

**Everything except T3 and T4 runs in under two seconds and costs no tokens.** Synthesis
itself takes tens of seconds and tens of thousands of tokens — verification is nearly free
in the cost structure.

The conclusion looks clear: **turn T1/T2/T5 plus coverage plus mutation all on, with no
reason to economise.** Only T3 and T4 need a value judgement.

> In practice this conclusion did not survive contact with measurement. Cheap is not the
> same as useful: those gates ran for free and never caught anything, while adding
> parameters to tune and false positives to diagnose. See the README.

---

## 13. Open questions

1. **The 80% mutation threshold is a guess.** It needs measuring: too high and it blocks
   correct code (some mutants are equivalent and cannot be killed semantically), too low
   and it is decoration. Identifying equivalent mutants is itself a hard problem.
2. **The cost of a false positive from a metamorphic property.** A property the model
   proposed may be wrong, and rejecting correct code with it makes the system stick where
   it was right. The current backstop is "only counts once the caller confirms it", but
   confirming has a cost of its own and the caller can click the wrong thing.
3. **What N should T3 use, and how do you make the implementations genuinely
   independent?** Rewording a prompt for the same model may leave the correlation very
   high. Different models lower it, at the cost of money and engineering complexity.
4. **The interruption cost of adjudication.** A differential adjudication has to interrupt
   the caller: for an agent that is an extra round of inference, for a person it is an
   interruption. Batch them up and ask once, or ask as each one is found?
5. **Is schema-driven input generation enough for complex nested structures?** Simple
   shapes are fine; deep nesting plus cross-field constraints ("when `type` is refund,
   `amount` must be negative") cannot be generated validly from a schema alone, and may
   need the caller to supply a generator or a constraint description.
6. **The gap between the real and the generated distribution.** T2, T3 and T5 all run on
   generated inputs. If real data is shaped very differently from the generator, these
   gates fail together — and T4, the only hedge, happens to be the most expensive one.
