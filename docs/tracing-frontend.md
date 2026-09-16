# Tracing front end (M4, not started)

| Field | Value |
| --- | --- |
| Status | Deferred — reassess once M0–M3 of [design.md](design.md) are working |
| Origin | The surviving part of the v0.1 design |

## What it solves

The back end described in [design.md](design.md) rests on one premise: **somebody tells
it "compile this".**

In the MVP that somebody is the agent itself (steered by a prompt) or a person. That is
unreliable — an agent head-down in its work is in no position to notice "this is the
seventh time I have done this; it should be a function". **Neither people nor agents are
good at spotting their own repetition.**

The tracing front end spots it for them and calls `compile_function` on their behalf. It
changes nothing about the back end; it just produces the back end's input automatically:
a requirement text plus examples.

```
watch the agent run -> spot repeated subtasks -> generate requirement + examples -> compile_function
```

**The key point: a trace is already an example.** [design.md §6.2](design.md) stakes the
whole correctness story on "the caller is willing to supply examples", which is the back
end's largest uncertainty. Every trace carries a real input/output pair of its own — the
front end turns the back end's most fragile assumption into a by-product. That is the
**primary** reason to build it, far more so than saving a manual trigger.

## Components

### Tracer

Hooks into the agent's tool-call path and records structured events asynchronously. It
must not affect the main flow if it fails, and its overhead must be negligible
(configurable sampling; large results stored as a digest only).

```python
@dataclass
class TraceEvent:
    trace_id: str; seq: int; ts: float
    kind: Literal["tool_call","tool_result","llm_step","task_begin","task_end"]
    tool: str | None
    args: dict                              # after redaction
    result_digest: str                      # large results are stored as a hash
    result_preview: Any                     # truncated, enough to read the semantics
    provenance: dict[str, ValueOrigin]      # see below
    cost: Cost
    effects: EffectClass
```

Redaction is a hard requirement: traces record tool arguments and results, which may
contain credentials and PII. Known credential field names are dropped outright, values
go through secret detection, and the trace store gets a retention period (30 days by
default) and access control.

### Subtask boundaries

| Strategy | Source | Reliability |
| --- | --- | --- |
| Explicit | the agent declares `with jit.subtask("pull the monthly report")` | high |
| Structural | the natural boundaries of a plan step, a TODO item, a subagent call | medium-high |
| Mined | frequent contiguous subsequences mined from the tool-call stream | medium |

Do the first two first. Explicit and structural boundaries come with a semantic label,
and that label is already a first draft of `compile_function`'s `requirement`. A purely
mined subsequence has no name, so a model has to guess what it was doing — much noisier.

### Hotspot detection

Not a call count — **cost-weighted**:

```
hotness(sig) = Σ (tokens_spent + λ · wall_clock)

trigger when:
  hotness > K · estimated_compile_cost     # K ≈ 3: only compile at an expected 3x payback
  AND observations >= 3                    # fewer than 3 makes anti-unification unreliable
  AND variance < V_max                     # traces too different means this is not one thing
  AND max_effect_class <= IDEMPOTENT_WRITE
```

**Better to compile too little than to produce something low-quality.** The cost of a
wrong artefact (quietly producing wrong results, plus the debugging) far exceeds the cost
of an uncompiled hotspot (it is just slow).

### Deciding the parameters: provenance beats inference

Given `read("/data/2026-08/a.csv") → write("/out/2026-08.json")`, which parts are
parameters? Asking a model gets most of them right, and the ones it gets wrong are very
hard to notice. The tracer records where every value came from, so the answer is
**read off, not guessed**:

| `ValueOrigin` | Verdict |
| --- | --- |
| `USER_INPUT` — from the user or an upstream task | a parameter |
| `UPSTREAM_OUTPUT` — from an earlier tool in this trace | an intermediate; compile it into the code |
| `ENV` — from the environment (cwd, today's date, config) | varies across traces → parameter; otherwise → an environment assertion |
| `LITERAL` — a constant the model wrote out of nowhere | a constant; promote to a parameter if it ever varies across traces |

Pair it with anti-unification for a **double confirmation**: only trigger a compile
automatically when the two agree; when they disagree, collect more traces or hand it to a
person. Cheap, and high return.

### Trace normalisation

Before computing a signature or anti-unifying, normalise:

1. **Value abstraction** — concrete values become typed placeholders (paths, URLs, dates
   and IDs each their own class)
2. **Dropping irrelevant steps** — failed retries are dropped by default; exploratory
   read-only calls are kept (they may carry actual control-flow decisions). Needs
   measurement to tune
3. **Order normalisation** — independent parallel calls are sorted into a canonical
   order; the dependencies come straight out of the provenance graph
4. **Loop folding** — `read(a) read(b) read(c)` → `for x in [a,b,c]: read(x)`. Rolling an
   unrolled loop back up, the counterpart of loop detection in a tracing JIT

### Anti-unification

Align N traces sharing a signature; the positions where the structure matches but the
values differ are the parameter candidates. This is a deterministic algorithm, not a
model guess — the model is only responsible for the final code synthesis, a much narrower
task and correspondingly more reliable.

## Generating the back end's input

```
a cluster of traces ──┬─→ requirement  ← semantic label + normalised skeleton + parameter
                      │                  table (an LLM turns it into prose)
                      └─→ examples     ← each trace's (input parameters, final output)
```

Hold one out: of N traces, one takes no part in synthesis and is used only for
acceptance. This is the most basic defence against overfitting — it stops the synthesiser
from memorising the samples. The trade-off in [design.md §11](design.md) — "too few
examples to be willing to hold one out" — does not exist here: traces keep accumulating.

## Open questions

1. **How do you quantify `variance` in a way that holds up?** Edit distance is a start,
   but "one extra retry" and "one missing critical step" can be the same distance apart
   while meaning very different things.
2. **Interception versus a tool.** Match and replace the agent's execution step
   automatically (transparent to the agent, zero prompt cost, but a mismatch is dangerous
   and the agent does not know it was swapped), or keep the agent calling explicitly? The
   suggestion is to upgrade to interception only for high-confidence hits (exact
   signature match, thorough verification, low guard-failure rate) — the counterpart of a
   JIT moving from conservative to aggressive inlining.
3. **How invasive the tracer is.** It needs the host agent to expose tool-call hooks.
   Frameworks differ a lot here, so it may need adapting one by one; this is the main
   compromise in positioning it as a bolt-on component.
