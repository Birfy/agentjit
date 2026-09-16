"""落盘：把合成出来的函数存起来，之后按名字取回。

布局：

    $AGENTJIT_HOME/registry/<spec_hash>/
        spec.json     名字 + 需求原文 + 推断出的 schema + 入口
        tests.json    调用方给的用例 —— 正确性就靠它，所以它是这里的主角
        v1/
            code.py       实现
            report.json   当时的验证报告

**用例是资产，代码是可再生的**（correctness.md §10）。所以 `tests.json` 属于 spec
不属于版本：同一个需求的所有版本共用同一份用例，换个更好的模型重新生成时拿它验收。
放在版本里意味着每次重生成都复制一份，之后各自增长，再也合不回去。

一个 `spec_hash` 下可以有多个版本，**取最新的那个**。早先这里按
(验证等级, 复验通过次数, 线上失败率) 三级排序，还有连续失败就隔离的逻辑 ——
真实使用中从来只存在过一个版本，那套排序一次也没派上用场，删了。
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

from .types import Example, Level, Report, Spec

HANDLE_RE = re.compile(r"^(?:fn_)?([0-9a-f]{6,64})$")

# 只有拿得出用例的才进持久缓存。EPHEMERAL 的定义就是"没有用例可判"，
# 存下来等于把一个没人验过的实现摆上货架。
CACHEABLE = (Level.VERIFIED,)


def home() -> Path:
    return Path(os.environ.get("AGENTJIT_HOME") or Path.home() / ".agentjit")


def _canon(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      default=str)


def spec_hash(requirement: str, spec: Spec) -> str:
    """缓存的 key：需求文本压掉空白 + 推断出的 schema + 入口名，精确 hash。

    换一种说法描述同一件事不会命中这里 —— 那条路在 `lookup.py` 的 L2。

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


@dataclass
class TestSet:
    """调用方给的用例。只增不减 —— 删一条就是把对需求的一点理解丢掉。"""

    examples: list[Example] = field(default_factory=list)

    def add(self, ex: Example) -> bool:
        """同一个输入只留一条。返回 True 表示这是条新的。"""
        key = _canon(ex.input)
        if any(_canon(e.input) == key for e in self.examples):
            return False
        self.examples.append(ex)
        return True

    def to_dict(self) -> dict[str, Any]:
        return {"examples": [e.to_dict() for e in self.examples]}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TestSet":
        return cls(examples=[Example.from_dict(e) for e in d.get("examples", [])])


@dataclass
class Version:
    name: str                        # "v1"
    level: Level
    code: str
    report: Report | None = None
    created_at: str = ""
    model: str = ""
    attempts: int = 1
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def n(self) -> int:
        return int(self.name[1:])

    def meta(self) -> dict[str, Any]:
        return {"name": self.name, "level": self.level.value,
                "created_at": self.created_at, "model": self.model,
                "attempts": self.attempts,
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens}


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

    def version(self, name: str) -> Version | None:
        return next((v for v in self.versions if v.name == name), None)

    def best(self) -> Version | None:
        """最新的那个。新版本多半是因为老的不够好才生成的。"""
        return max(self.versions, key=lambda v: v.n, default=None)

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
        m = HANDLE_RE.match(base)
        if not m:
            return None
        h = m.group(1)
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
        return sorted((self._load(d) for d in self._dirs()),
                      key=lambda f: f.created_at, reverse=True)

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
                name=vm["name"], level=Level(vm["level"]),
                code=(vd / "code.py").read_text(), report=report,
                created_at=vm.get("created_at", ""), model=vm.get("model", ""),
                attempts=vm.get("attempts", 1),
                input_tokens=vm.get("input_tokens", 0),
                output_tokens=vm.get("output_tokens", 0),
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
                f"{report.level.value} 不进持久缓存 —— 没有用例验过的实现存下来，"
                "就是把一个没人验过的东西摆上货架")

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
            fn.tests.add(ex)
        self.save_tests(fn)

        v = Version(name=f"v{max((x.n for x in fn.versions), default=0) + 1}",
                    level=report.level, code=code, report=report, created_at=_now(),
                    model=model, attempts=attempts,
                    input_tokens=input_tokens, output_tokens=output_tokens)
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
