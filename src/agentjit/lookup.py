"""三级查找。见 [design.md §6.1](../../docs/design.md#61-规格归一化缓存的-key-不是原文)。

    L1 spec_hash 精确匹配 → L2 候选检索 + **用本次例子复验** → L3 miss，去合成。

**最要紧的一条结论放在最前面：检索质量只影响命中率，不影响正确性。**

L2 命中必须用本次请求的例子跑一遍复验，跑不过就当 miss。所以检索可以很土 ——
土的检索只是少命中几次，绝不会交出一个错的函数。design.md §6.1 说 L2 用向量近邻，
这里先用**字符二元组重合度**顶着：中文没有现成的分词，字符 n-gram 是这个量级下
最靠谱的土办法，而且不需要网络、不花 token。向量索引是 registry 大到线性扫不动
之后的优化，不是正确性机制 —— 把这两件事分清楚，L2 就不吓人了。

和文档字面不同的一处：**L1 命中也复验。** 文档说 L1"直接用"。但复验只要一次沙箱
运行、不花 token，而它挡住的是一个真实场景：同一段需求文本，调用方这次带来的
例子和上次不一样（上次的期望本身写错了，或者需求被重新理解了）。这时候直接用
旧版本，就是拿一个已知不满足本次判据的实现去交差。复验不过就合成新版本 ——
design.md §4.1 的 `reused_with_new_version` 说的正是这件事。
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

import jsonschema

from .registry import Function, Registry, Version, spec_hash
from .sandbox import Sandbox
from .types import Example, Spec, deep_equal

# 相似度低于这个数的候选连复验都不值得跑。宁可漏 —— 漏了只是去合成一次，
# 而每个候选的复验都要起一次沙箱。
MIN_SIMILARITY = 0.20

# 最多复验几个候选。按相似度降序取前几个。
MAX_CANDIDATES = 5

_PUNCT = re.compile(r"[\s　-〿＀-￯!-/:-@\[-`{-~]+")


def normalize(text: str) -> str:
    """把一句需求压成可比较的形式。

    NFKC（全角半角、兼容字符）→ 小写 → 去掉所有标点和空白。做不了的是语义：
    "求和"和"求总额"在这里仍然是两个串。那一层交给复验，不交给字符串。
    """
    return _PUNCT.sub("", unicodedata.normalize("NFKC", text).lower())


def bigrams(text: str) -> set[str]:
    s = normalize(text)
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def similarity(a: str, b: str) -> float:
    """Dice 系数。两句话完全一样是 1，毫无重合是 0。"""
    x, y = bigrams(a), bigrams(b)
    if not x or not y:
        return 1.0 if x == y else 0.0
    return 2 * len(x & y) / (len(x) + len(y))


def _fits(value: Any, schema: dict) -> str:
    try:
        jsonschema.validate(value, schema)
        return ""
    except jsonschema.ValidationError as e:
        return f"{'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}"
    except jsonschema.SchemaError as e:
        return f"schema 本身有问题: {e.message}"


def schema_compatible(spec: Spec, examples: list[Example]) -> str:
    """本次的例子能不能套进候选的 schema。空字符串 = 能。

    用本次例子去验候选的 schema，而不是比较两份 schema 的结构 —— 后者要定义
    "兼容"是什么意思，前者直接问了真正要回答的问题：这些输入喂进去合法吗。
    而且它和运行时那道入参 guard 用的是同一把尺子，不会出现"查找说兼容、
    调用时被 guard 拦下"这种自相矛盾。
    """
    for i, ex in enumerate(examples):
        if msg := _fits(ex.input, spec.param_schema):
            return f"例 {i} 的输入不合候选的 param_schema —— {msg}"
        if msg := _fits(ex.output, spec.return_schema):
            return f"例 {i} 的期望输出不合候选的 return_schema —— {msg}"
    return ""


@dataclass
class Candidate:
    fn: Function
    version: Version
    similarity: float
    verdict: str = "pending"     # pending | schema | reverify | hit
    why: str = ""

    @property
    def hit(self) -> bool:
        return self.verdict == "hit"

    def render(self) -> str:
        tag = {"hit": "命中", "schema": "schema 不兼容", "reverify": "复验没过",
               "pending": "没轮到"}.get(self.verdict, self.verdict)
        line = f"  {self.fn.handle}@{self.version.name}  相似度 {self.similarity:.2f}  {tag}"
        return line + (f"\n      {self.why}" if self.why else "")


@dataclass
class Lookup:
    level: str                               # L1 | L2 | miss
    fn: Function | None = None
    version: Version | None = None
    candidates: list[Candidate] = field(default_factory=list)
    stale: Function | None = None            # L1 命中了但复验没过的那个函数

    @property
    def hit(self) -> bool:
        return self.fn is not None

    def render(self) -> str:
        head = (f"{self.level} 命中 {self.fn.handle}@{self.version.name}" if self.hit
                else f"{self.level}：没有可用的现成函数")
        if not self.candidates:
            return head
        return head + "\n" + "\n".join(c.render() for c in self.candidates)


def find(
    reg: Registry,
    requirement: str,
    spec: Spec,
    examples: list[Example],
    *,
    sandbox: Sandbox | None = None,
    min_similarity: float = MIN_SIMILARITY,
    max_candidates: int = MAX_CANDIDATES,
) -> Lookup:
    """查一遍缓存。**只读** —— 命中之后要记的账由调用方落盘（见 jit.py）。"""
    sb = sandbox or Sandbox()
    own = spec_hash(requirement, spec)

    # --- L1：同一个说法 ----------------------------------------------------
    exact = reg.get(f"fn_{own}")
    if exact is not None and (v := exact.best()) is not None:
        cand = Candidate(exact, v, 1.0)
        if reverify(cand, examples, sb):
            return Lookup("L1", exact, v, [cand])
        # 同一段需求，这次的例子和上次不一样 —— 旧版本已经不满足本次判据了
        return Lookup("miss", candidates=[cand], stale=exact)

    # --- L2：别的说法 ------------------------------------------------------
    scored = []
    for fn in reg.all():
        if fn.spec_hash == own or (v := fn.best()) is None:
            continue
        s = similarity(requirement, fn.requirement)
        if s >= min_similarity:
            scored.append(Candidate(fn, v, s))
    scored.sort(key=lambda c: -c.similarity)

    tried = scored[:max_candidates]
    for cand in tried:
        if why := schema_compatible(cand.fn.spec, examples):
            cand.verdict, cand.why = "schema", why
            continue
        if reverify(cand, examples, sb):
            return Lookup("L2", cand.fn, cand.version, tried)

    return Lookup("miss", candidates=tried)


def reverify(cand: Candidate, examples: list[Example], sb: Sandbox) -> bool:
    """[design.md §6.2](../../docs/design.md#62-例子即规格--本设计的核心主张) 里那道免费的 guard。

    拿本次调用方自己的例子跑候选一遍。用的是调用方的标准，不是我们的 ——
    所以"求和"的函数遇到"求平均"的例子必然挂，两个在嵌入空间里挨得多近都没用。
    """
    if not examples:
        cand.verdict, cand.why = "reverify", "本次没给例子，复验无从谈起"
        return False

    spec = cand.fn.spec
    run = sb.run(cand.version.code, spec.entry, [e.input for e in examples],
                 timeout_ms=spec.timeout_ms, mem_mb=spec.mem_mb)
    if why := run.why_dead:
        cand.verdict, cand.why = "reverify", f"候选跑不起来：{why}"
        return False

    for i, (ex, r) in enumerate(zip(examples, run.results)):
        if not r.ok:
            cand.verdict, cand.why = "reverify", f"例 {i} 崩了：{r.error}"
            return False
        if not deep_equal(r.value, ex.output):
            cand.verdict, cand.why = "reverify", (
                f"例 {i} 对不上：期望 {ex.output!r}，实际 {r.value!r}")
            return False

    cand.verdict = "hit"
    return True


def search(reg: Registry, query: str, limit: int = 10) -> list[Candidate]:
    """`search_functions` —— 写需求之前先看看有没有现成的。

    只排序不复验：这里回答的是"有没有像的"，不是"能不能用"。
    后者要例子，而 search 的调用方还没写例子。
    """
    out = []
    for fn in reg.all():
        v = fn.best()
        if v is None:
            continue
        out.append(Candidate(fn, v, similarity(query, fn.requirement)))
    out.sort(key=lambda c: -c.similarity)
    return out[:limit]
