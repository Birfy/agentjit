"""持久化 Registry —— 以**测试集**为中心。

见 [correctness.md §10](../../docs/correctness.md#10-测试集是资产代码是可再生的)：
一个函数的核心资产不是代码，是测试集。代码可以随时用更好的模型重新生成，测试集
是一点一点攒出来的。所以这里存的主角是 `tests.json`，`code.py` 只是"当前通过
它的一个实现"。

布局：

    $AGENTJIT_HOME/registry/<spec_hash>/
        spec.json     需求原文 + 推断出的 schema + 入口（spec_hash 的来源）
        tests.json    测试集：examples（有答案）+ probes（没答案，但不许崩）
        v1/
            code.py       实现
            report.json   当时的验证报告
            meta.json     等级、合成成本、运行时统计、是否被隔离

**tests.json 放在 spec 一层而不是版本一层**，和 NEXT.md 里画的目录不同。理由就是
上面那条：同一个 spec 的所有版本共用同一份单调增长的测试集，"模型升级 = 免费的
全库重生成"要成立，测试集就必须是版本之外的单一事实来源。放在版本里意味着每次
重生成都得复制一份，之后两份各自增长，再也合不回去。

`QUARANTINED` 在文档的等级表里和 `VERIFIED` 并列，但它是**运行时状态**不是验证
结论 —— `verify()` 永远不会返回它。所以它落在版本的 `state` 上，而不是 `Level` 里。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from . import assertions
from .types import Example, Level, Report, Spec

HANDLE_RE = re.compile(r"^(?:fn_)?([0-9a-f]{6,64})(?:@(v\d+))?$")

# 只有拿得出验收判据的才进持久缓存。EPHEMERAL 的定义就是"拿不出判据"，
# 存下来等于把一个没人验过的实现摆上货架 —— 见 design.md §8.1 的可缓存列。
CACHEABLE = (Level.VERIFIED, Level.CONFIRMED)

_LEVEL_RANK = {Level.CONFIRMED: 3, Level.VERIFIED: 2, Level.EPHEMERAL: 1, Level.REJECTED: 0}


def home() -> Path:
    return Path(os.environ.get("AGENTJIT_HOME") or Path.home() / ".agentjit")


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      default=str)


def spec_hash(requirement: str, spec: Spec) -> str:
    """缓存的 key。

    M0 只做**精确** hash：需求文本压掉空白，加上推断出的 schema 和入口名。
    换一种说法描述同一件事不会命中 —— 那要 `CanonicalSpec` 和三级查找，是 M1
    （NEXT.md §4）。先把"同一个说法能命中"做对，再去做"不同说法也能命中"。

    schema 参与 hash 不是多余的：同样一句"求和"，输入是 `{rows: [...]}` 还是
    `{values: [...]}`，要的是两个不同的函数。
    """
    norm = " ".join(requirement.split())
    blob = _canon([norm, spec.param_schema, spec.return_schema, spec.entry])
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def split_ref(ref: str) -> tuple[str, str | None]:
    """把 `<名字或 handle>[@版本]` 拆开。

    名字和 handle 走同一条路 —— 调用方不该为了指定版本先判断自己拿的是哪种。
    `rank@v2` / `fn_7a3c9e@v2` / `rank` / `fn_7a3c9e` 都行。
    """
    base, _, ver = ref.strip().partition("@")
    return base.strip(), (ver.strip() or None)


def parse_handle(handle: str) -> tuple[str, str | None]:
    """`fn_7a3c9e` → (hash, None)；`fn_7a3c9e@v2` → (hash, 'v2')。名字不走这里。"""
    m = HANDLE_RE.match(handle.strip())
    if not m:
        raise ValueError(f"handle 格式不对: {handle!r}（应形如 fn_7a3c9e 或 fn_7a3c9e@v2）")
    return m.group(1), m.group(2)


# --- 测试集 ----------------------------------------------------------------
@dataclass
class Probe:
    """一个把某个版本弄挂过的输入。

    它没有标准答案 —— 线上抓到的失败输入，谁也不知道正确结果该是什么。但
    "不许崩、返回值得合 return_schema"本身就是判据（correctness.md §7 的 T5），
    这已经足够让它当一条永久回归用例用。等有人补上答案，它就升级成 Example。
    """

    input: dict[str, Any]
    kind: str                     # runtime_error | postcondition | budget | guard
    detail: str = ""
    seen: int = 1
    first_seen: str = ""
    version: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"input": self.input, "kind": self.kind, "detail": self.detail,
                "seen": self.seen, "first_seen": self.first_seen, "version": self.version}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Probe":
        return cls(input=d["input"], kind=d.get("kind", "runtime_error"),
                   detail=d.get("detail", ""), seen=d.get("seen", 1),
                   first_seen=d.get("first_seen", ""), version=d.get("version", ""))


# 每类探针的上限。超了先丢"只见过一次"的 —— 见 TestSet.add_probe。
MAX_PROBES_PER_KIND = 200


@dataclass
class TestSet:
    """判据只增不减；探针有上限。

    `examples` 是 oracle，永不删除 —— 删一条就是把对需求的一点理解丢掉。
    `probes` 是事故记录，管着上限：design.md §6.5 那句"缓存腐化比缓存未命中更伤"
    对测试集同样成立，一个装满一次性垃圾输入的测试集会淹掉真正该看的那几条。
    """

    examples: list[Example] = field(default_factory=list)
    probes: list[Probe] = field(default_factory=list)

    def add_example(self, ex: Example) -> bool:
        key = _canon(ex.input)
        if any(_canon(e.input) == key for e in self.examples):
            return False
        self.examples.append(ex)
        return True

    def add_probe(self, p: Probe) -> bool:
        """同一个输入再挂一次只记次数 —— 一条线上 bug 被打一万次，
        测试集也只该厚一条。返回 True 表示这是个没见过的输入。"""
        key = _canon(p.input)
        for old in self.probes:
            if _canon(old.input) == key:
                old.seen += 1
                old.kind, old.detail, old.version = p.kind, p.detail, p.version
                return False
        p.first_seen = p.first_seen or _now()
        self.probes.append(p)
        self._evict(p.kind)
        return True

    def _evict(self, kind: str) -> None:
        """超上限时丢掉复现次数最少、最早见到的那条。

        反复出现的输入是真信号，只见过一次的多半是一次性噪声。留谁不留谁按
        "被打中几次"排，比按时间排靠谱。
        """
        same = [p for p in self.probes if p.kind == kind]
        if len(same) <= MAX_PROBES_PER_KIND:
            return
        drop = sorted(same, key=lambda p: (p.seen, p.first_seen))[: len(same) - MAX_PROBES_PER_KIND]
        dead = {id(p) for p in drop}
        self.probes = [p for p in self.probes if id(p) not in dead]

    def to_dict(self) -> dict[str, Any]:
        return {"examples": [e.to_dict() for e in self.examples],
                "probes": [p.to_dict() for p in self.probes]}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TestSet":
        return cls(examples=[Example.from_dict(e) for e in d.get("examples", [])],
                   probes=[Probe.from_dict(p) for p in d.get("probes", [])])


# --- 版本 ------------------------------------------------------------------
@dataclass
class Stats:
    """一个版本的运行时账本。"""

    calls: int = 0
    ok: int = 0
    guard_failed: int = 0            # 入参没过 schema —— 没进函数，不算它的错
    runtime_error: int = 0
    postcondition_failed: int = 0
    budget_exceeded: int = 0
    warnings: int = 0                # 后置断言违反（只警告，见 assertions.py）
    consecutive_failures: int = 0
    reverify_passes: int = 0
    saved: float = 0.0               # 累计省下的加权 token，见 econ.py
    last_used: str = ""

    @property
    def attributable(self) -> int:
        """能算到这个版本头上的失败次数。入参 guard 不在其中。"""
        return self.runtime_error + self.postcondition_failed + self.budget_exceeded

    @property
    def failure_rate(self) -> float:
        graded = self.ok + self.attributable
        return self.attributable / graded if graded else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Stats":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class Version:
    name: str                        # "v1"
    level: Level
    code: str
    report: Report | None = None
    state: str = "active"            # active | quarantined
    quarantine_reason: str = ""
    created_at: str = ""
    model: str = ""
    attempts: int = 1
    synth_input_tokens: int = 0
    synth_output_tokens: int = 0
    # 从例子里挖出来的后置断言（assertions.py）。只警告不阻断。
    properties: list[str] = field(default_factory=list)
    stats: Stats = field(default_factory=Stats)

    @property
    def n(self) -> int:
        return int(self.name[1:])

    @property
    def active(self) -> bool:
        return self.state == "active"

    def meta(self) -> dict[str, Any]:
        return {"name": self.name, "level": self.level.value, "state": self.state,
                "quarantine_reason": self.quarantine_reason,
                "created_at": self.created_at, "model": self.model,
                "attempts": self.attempts,
                "synth_input_tokens": self.synth_input_tokens,
                "synth_output_tokens": self.synth_output_tokens,
                "properties": list(self.properties),
                "stats": self.stats.to_dict()}


@dataclass
class Function:
    spec_hash: str
    requirement: str
    spec: Spec
    tests: TestSet
    versions: list[Version] = field(default_factory=list)
    created_at: str = ""
    # 人起的名字。`fn_a84dbc11d69f` 机器好用，人记不住 —— 而这东西的用法就是
    # "上次那个排名次的函数叫什么来着"。没起名字就只能靠 handle。
    name: str = ""

    @property
    def handle(self) -> str:
        return f"fn_{self.spec_hash}"

    @property
    def ref(self) -> str:
        """指代它的最短方式。有名字用名字。"""
        return self.name or self.handle

    @property
    def quarantined(self) -> bool:
        return bool(self.versions) and not any(v.active for v in self.versions)

    def version(self, name: str) -> Version | None:
        return next((v for v in self.versions if v.name == name), None)

    def best(self) -> Version | None:
        """按 (验证等级, 复验通过次数, guard 失败率) 取最优，见 design.md §6.5。

        被隔离的版本不参与 —— 隔离的意思就是"别再用它了"。同分时取新的：
        新版本多半是因为老版本不够好才生成的。
        """
        live = [v for v in self.versions if v.active]
        if not live:
            return None
        return max(live, key=lambda v: (_LEVEL_RANK.get(v.level, 0),
                                        v.stats.reverify_passes,
                                        -v.stats.failure_rate,
                                        v.n))

    def spec_meta(self) -> dict[str, Any]:
        return {"spec_hash": self.spec_hash, "name": self.name,
                "requirement": self.requirement,
                "spec": self.spec.to_dict(), "created_at": self.created_at}


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())


def _write(path: Path, text: str) -> None:
    """原子落盘。半截的 tests.json 比没有 tests.json 更难查。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _dump(path: Path, obj: Any) -> None:
    _write(path, json.dumps(obj, ensure_ascii=False, indent=2, default=str) + "\n")


class NotCacheable(ValueError):
    pass


class NameTaken(ValueError):
    pass


class Registry:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else home() / "registry"

    # --- 读 ----------------------------------------------------------------
    def dir_of(self, spec_hash: str) -> Path:
        return self.root / spec_hash

    def get(self, ref: str) -> Function | None:
        """按**名字**或 handle 取一个函数。

        名字优先 —— 人给的名字不会长得像 `fn_a84dbc11`，撞不上；真撞上了说明
        用户就是想按那个名字找。
        """
        base, _ = split_ref(ref)
        if (fn := self.by_name(base)) is not None:
            return fn
        try:
            h, _ = parse_handle(base)
        except ValueError:
            return None
        d = self.dir_of(h)
        if not (d / "spec.json").exists():
            # 允许用前缀找：handle 在终端里被截断是常事
            matches = [p for p in self._dirs() if p.name.startswith(h)]
            if len(matches) != 1:
                return None
            d = matches[0]
        return self._load(d)

    def by_name(self, name: str) -> Function | None:
        if not name:
            return None
        for d in self._dirs():
            meta = json.loads((d / "spec.json").read_text())
            if meta.get("name") == name:
                return self._load(d)
        return None

    def all(self) -> list[Function]:
        fns = [self._load(d) for d in self._dirs()]
        return sorted(fns, key=lambda f: f.created_at, reverse=True)

    def _dirs(self) -> Iterator[Path]:
        if not self.root.exists():
            return iter(())
        return (d for d in sorted(self.root.iterdir()) if (d / "spec.json").exists())

    def _load(self, d: Path) -> Function:
        meta = json.loads((d / "spec.json").read_text())
        tests = TestSet.from_dict(json.loads((d / "tests.json").read_text())
                                  if (d / "tests.json").exists() else {})
        versions = []
        for vd in sorted(d.glob("v*"), key=lambda p: int(p.name[1:]) if p.name[1:].isdigit() else 0):
            if not (vd / "meta.json").exists():
                continue
            vm = json.loads((vd / "meta.json").read_text())
            report = None
            if (vd / "report.json").exists():
                report = Report.from_dict(json.loads((vd / "report.json").read_text()))
            versions.append(Version(
                name=vm["name"], level=Level(vm["level"]), state=vm.get("state", "active"),
                quarantine_reason=vm.get("quarantine_reason", ""),
                code=(vd / "code.py").read_text(), report=report,
                created_at=vm.get("created_at", ""), model=vm.get("model", ""),
                attempts=vm.get("attempts", 1),
                synth_input_tokens=vm.get("synth_input_tokens", 0),
                synth_output_tokens=vm.get("synth_output_tokens", 0),
                properties=list(vm.get("properties") or []),
                stats=Stats.from_dict(vm.get("stats") or {}),
            ))
        return Function(spec_hash=meta["spec_hash"], requirement=meta["requirement"],
                        spec=Spec.from_dict(meta["spec"]), tests=tests,
                        versions=versions, created_at=meta.get("created_at", ""),
                        name=meta.get("name", ""))

    # --- 写 ----------------------------------------------------------------
    def put(
        self,
        requirement: str,
        spec: Spec,
        code: str,
        report: Report,
        examples: list[Example],
        *,
        model: str = "",
        attempts: int = 1,
        input_tokens: int = 0,
        output_tokens: int = 0,
        name: str = "",
    ) -> Function:
        """把一次成功的合成落盘。已有同 spec 时追加一个新版本。"""
        if report.level not in CACHEABLE:
            raise NotCacheable(
                f"{report.level.value} 不进持久缓存 —— 拿不出验收判据的实现存下来"
                "就是把一个没人验过的东西摆上货架（design.md §8.1）")

        h = spec_hash(requirement, spec)
        if name and (other := self.by_name(name)) is not None and other.spec_hash != h:
            # 名字是给人用的索引，一个名字指向两个函数就等于没索引。
            # 宁可在这里报错，也不要让 get("rank") 的结果取决于目录遍历顺序。
            raise NameTaken(f"名字 {name!r} 已经被 {other.handle} 占了"
                            f"（{other.requirement[:40]}）—— 换一个，或者不起名字")

        fn = self._load(self.dir_of(h)) if (self.dir_of(h) / "spec.json").exists() else None
        if fn is None:
            fn = Function(spec_hash=h, requirement=requirement, spec=spec,
                          tests=TestSet(), created_at=_now(), name=name)
            self.save_spec(fn)
        elif name and fn.name != name:
            fn.name = name                       # 补个名字，或者改名
            self.save_spec(fn)

        for ex in examples:
            fn.tests.add_example(ex)
        self.save_tests(fn)

        name = f"v{max((v.n for v in fn.versions), default=0) + 1}"
        v = Version(name=name, level=report.level, code=code, report=report,
                    created_at=_now(), model=model, attempts=attempts,
                    synth_input_tokens=input_tokens, synth_output_tokens=output_tokens,
                    # 性质从**整个**测试集挖，不只是本次带来的那几个例子：
                    # 测试集越厚，挖出来的性质越可信，巧合越少。
                    properties=assertions.mine(fn.tests.examples))
        fn.versions.append(v)
        self.save_version(fn, v)
        return fn

    def save_spec(self, fn: Function) -> None:
        _dump(self.dir_of(fn.spec_hash) / "spec.json", fn.spec_meta())

    def save_tests(self, fn: Function) -> None:
        _dump(self.dir_of(fn.spec_hash) / "tests.json", fn.tests.to_dict())

    def save_version(self, fn: Function, v: Version) -> None:
        vd = self.dir_of(fn.spec_hash) / v.name
        vd.mkdir(parents=True, exist_ok=True)
        _write(vd / "code.py", v.code if v.code.endswith("\n") else v.code + "\n")
        if v.report is not None:
            _dump(vd / "report.json", v.report.to_dict())
        _dump(vd / "meta.json", v.meta())

    def quarantine(self, fn: Function, v: Version, why: str = "") -> None:
        """连续失败到门槛 → 这个版本不再被 best() 选中，下次请求重新合成。

        代码留着不删：它是"这个测试集当时接受了什么"的证据，排查线上分歧时要看。
        """
        v.state = "quarantined"
        v.quarantine_reason = why
        self.save_version(fn, v)
