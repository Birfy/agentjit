"""The static check: an AST allowlist.

The first gate, and the cheapest one. Code that fails it never reaches the sandbox —
that saves an execution, but more importantly it keeps the dumbest and most common
escape tricks outside the boundary.

This is **not** a complete security boundary. It is the first layer of defence in
depth; the second is the sandbox itself (restricted builtins + no I/O + rlimit). See
docs/design.md §7.
"""
from __future__ import annotations

import ast
import re

# Modules pre-injected into the execution namespace. Generated code uses them directly;
# there is no import.
INJECTED_MODULES = (
    "math", "re", "json", "datetime", "decimal",
    "statistics", "itertools", "functools", "collections",
)

BANNED_NAMES = frozenset({
    "eval", "exec", "compile", "open", "input", "__import__",
    "globals", "locals", "vars", "dir", "breakpoint", "exit", "quit",
    # getattr/setattr are the standard way around the dunder-attribute check:
    # getattr(x, "__class__")
    "getattr", "setattr", "delattr",
})

_DUNDER = re.compile(r"^__\w+__$")

# Literals that look like credentials — a hard reject. Credentials may only be injected
# by the facade runtime.
_SECRET_PATTERNS = (
    re.compile(r"\b(sk|pk|api[_-]?key|secret|token|passwd|password)\b[\"':= ]", re.I),
    re.compile(r"\b(ghp|gho|github_pat|xox[baprs])[_-][A-Za-z0-9]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
)


class Violation(Exception):
    pass


def check(source: str, entry: str = "solve") -> list[str]:
    """Return the list of violations; empty means it passed."""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [f"syntax error: {e}"]

    v: list[str] = []
    for node in ast.walk(tree):
        match node:
            case ast.Import() | ast.ImportFrom():
                v.append(f"L{node.lineno}: no import; "
                         f"{', '.join(INJECTED_MODULES)} are already available")
            case ast.Name(id=name) if name in BANNED_NAMES:
                v.append(f"L{node.lineno}: {name} is not allowed")
            case ast.Attribute(attr=attr) if attr.startswith("__"):
                v.append(f"L{node.lineno}: dunder attribute access .{attr} is not allowed")
            case ast.Constant(value=str() as s) if _DUNDER.match(s):
                v.append(f"L{node.lineno}: dunder literal {s!r} is not allowed "
                         "(the usual shape of a getattr escape)")
            case ast.AsyncFunctionDef() | ast.Await() | ast.AsyncFor() | ast.AsyncWith():
                v.append(f"L{node.lineno}: async is not supported")

    for pat in _SECRET_PATTERNS:
        if m := pat.search(source):
            v.append(f"looks like a hard-coded credential: {m.group(0)!r} — "
                     "credentials must be injected by the runtime through ctx")

    fns = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    if entry not in fns:
        v.append(f"missing entry point {entry}(params, ctx)")
    else:
        args = fns[entry].args
        names = [a.arg for a in args.posonlyargs + args.args]
        if len(names) != 2:
            v.append(f"{entry} must take exactly two parameters (params, ctx), got {names}")

    return v


# An ordinary data-processing function has no business growing an external endpoint. See
# docs/design.md §7.4 — even if an injection did get the model to write something
# suspicious, this puts it in front of a human.
_REVIEW_PATTERNS = (
    (re.compile(r"https?://[^\s\"']+"), "hard-coded URL"),
    (re.compile(r"[\"'](/(?:[\w.-]+/){1,}[\w.-]*)[\"']"), "hard-coded absolute path"),
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "hard-coded IP"),
)


def review_flags(source: str) -> list[str]:
    """Non-blocking, but worth putting in front of a human."""
    out = []
    for pat, why in _REVIEW_PATTERNS:
        for m in set(pat.findall(source)):
            out.append(f"{why}: {m if isinstance(m, str) else m[0]}")
    return out
