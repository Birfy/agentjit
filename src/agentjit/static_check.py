"""静态检查：AST 白名单。

第一道关卡，也是最便宜的一道。不过的代码根本不进沙箱 —— 省下一次执行，
更重要的是把最蠢也最常见的逃逸手法挡在外面。

这不是完整的安全边界。它是纵深防御的第一层，第二层是沙箱本身
（受限 builtins + 无 I/O + rlimit）。见 docs/design.md §7。
"""
from __future__ import annotations

import ast
import re

# 预注入到执行命名空间的模块。生成的代码直接用，不需要 import。
INJECTED_MODULES = (
    "math", "re", "json", "datetime", "decimal",
    "statistics", "itertools", "functools", "collections",
)

BANNED_NAMES = frozenset({
    "eval", "exec", "compile", "open", "input", "__import__",
    "globals", "locals", "vars", "dir", "breakpoint", "exit", "quit",
    # getattr/setattr 是绕过 dunder 属性检查的标准手法：getattr(x, "__class__")
    "getattr", "setattr", "delattr",
})

_DUNDER = re.compile(r"^__\w+__$")

# 疑似凭据的字面量 —— 硬性拒绝。凭据只能由 facade 运行时注入。
_SECRET_PATTERNS = (
    re.compile(r"\b(sk|pk|api[_-]?key|secret|token|passwd|password)\b[\"':= ]", re.I),
    re.compile(r"\b(ghp|gho|github_pat|xox[baprs])[_-][A-Za-z0-9]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)


class Violation(Exception):
    pass


def check(source: str, entry: str = "solve") -> list[str]:
    """返回违规列表。空列表 = 通过。"""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [f"语法错误: {e}"]

    v: list[str] = []
    for node in ast.walk(tree):
        match node:
            case ast.Import() | ast.ImportFrom():
                v.append(f"L{node.lineno}: 禁止 import；可直接使用 {', '.join(INJECTED_MODULES)}")
            case ast.Name(id=name) if name in BANNED_NAMES:
                v.append(f"L{node.lineno}: 禁止使用 {name}")
            case ast.Attribute(attr=attr) if attr.startswith("__"):
                v.append(f"L{node.lineno}: 禁止访问 dunder 属性 .{attr}")
            case ast.Constant(value=str() as s) if _DUNDER.match(s):
                v.append(f"L{node.lineno}: 禁止 dunder 字面量 {s!r}（getattr 逃逸的常见形式）")
            case ast.AsyncFunctionDef() | ast.Await() | ast.AsyncFor() | ast.AsyncWith():
                v.append(f"L{node.lineno}: M0 不支持 async")

    for pat in _SECRET_PATTERNS:
        if m := pat.search(source):
            v.append(f"疑似硬编码凭据: {m.group(0)!r} —— 凭据必须由 ctx 运行时注入")

    fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    if entry not in fns:
        v.append(f"缺少入口函数 {entry}(params, ctx)")
    else:
        args = fns[entry].args
        names = [a.arg for a in args.posonlyargs + args.args]
        if len(names) != 2:
            v.append(f"{entry} 必须接受恰好两个参数 (params, ctx)，实际 {names}")

    return v
