"""变异测试的算子。见 docs/correctness.md §8.2。

覆盖率只说明代码被执行过，不说明错误会被抓住。变异测试直接量化后者：
故意把代码改坏，看测试集能不能发现。抓不住的变异体就是测试集的盲区，
而且它明确告诉你盲区在哪 —— "把第 7 行的 > 改成 >= 没人发现"，
这就是缺失用例的精确描述。

变异测试在正常工程里太慢没人用。这里的函数是 20 行的纯函数，跑完只要几百毫秒 ——
这是纯函数设计的意外红利。
"""
from __future__ import annotations

import ast
import copy
import random
from dataclasses import dataclass

_CMP = {
    ast.Lt: [ast.LtE, ast.Gt], ast.LtE: [ast.Lt, ast.GtE],
    ast.Gt: [ast.GtE, ast.Lt], ast.GtE: [ast.Gt, ast.LtE],
    ast.Eq: [ast.NotEq], ast.NotEq: [ast.Eq],
    ast.In: [ast.NotIn], ast.NotIn: [ast.In],
    ast.Is: [ast.IsNot], ast.IsNot: [ast.Is],
}
_BIN = {
    ast.Add: [ast.Sub], ast.Sub: [ast.Add],
    ast.Mult: [ast.Add], ast.Div: [ast.Mult],
    ast.FloorDiv: [ast.Div], ast.Mod: [ast.Mult], ast.Pow: [ast.Mult],
}


@dataclass
class Mutant:
    index: int
    kind: str
    where: str
    source: str


def _const_variants(v):
    if isinstance(v, bool):
        return [not v]
    if isinstance(v, int):
        return [v + 1, v - 1, 0] if v != 0 else [1, -1]
    if isinstance(v, float):
        return [v + 1.0, 0.0] if v != 0.0 else [1.0]
    if isinstance(v, str):
        return ["" if v else "x"]
    return []


def _docstring_nodes(tree):
    out = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr) \
           and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            out.add(id(body[0].value))
    return out


def _candidates(tree) -> list[dict]:
    docs = _docstring_nodes(tree)
    cands: list[dict] = []
    for wi, node in enumerate(ast.walk(tree)):
        line = getattr(node, "lineno", 0)
        if isinstance(node, ast.Compare):
            for j, op in enumerate(node.ops):
                for new in _CMP.get(type(op), []):
                    cands.append({"wi": wi, "kind": "cmp", "j": j, "new": new,
                                  "where": f"L{line}: {type(op).__name__} -> {new.__name__}"})
        elif isinstance(node, ast.BinOp):
            for new in _BIN.get(type(node.op), []):
                cands.append({"wi": wi, "kind": "bin", "new": new,
                              "where": f"L{line}: {type(node.op).__name__} -> {new.__name__}"})
        elif isinstance(node, ast.AugAssign):
            for new in _BIN.get(type(node.op), []):
                cands.append({"wi": wi, "kind": "aug", "new": new,
                              "where": f"L{line}: {type(node.op).__name__}= -> {new.__name__}="})
        elif isinstance(node, ast.BoolOp):
            new = ast.Or if isinstance(node.op, ast.And) else ast.And
            cands.append({"wi": wi, "kind": "bool", "new": new,
                          "where": f"L{line}: and/or 互换"})
        elif isinstance(node, ast.Constant) and id(node) not in docs:
            for nv in _const_variants(node.value):
                cands.append({"wi": wi, "kind": "const", "new": nv,
                              "where": f"L{line}: {node.value!r} -> {nv!r}"})
        elif isinstance(node, ast.Return) and node.value is not None:
            cands.append({"wi": wi, "kind": "ret_none", "where": f"L{line}: return X -> return None"})
        elif isinstance(node, (ast.If, ast.While)):
            cands.append({"wi": wi, "kind": "invert", "where": f"L{line}: 条件取反"})

        for field in ("body", "orelse", "finalbody"):
            stmts = getattr(node, field, None)
            if isinstance(stmts, list) and len(stmts) > 1:
                for pos, st in enumerate(stmts):
                    # 删 docstring 是等价变异，会白白拉低分母
                    if isinstance(st, ast.Expr) and id(st.value) in docs:
                        continue
                    cands.append({"wi": wi, "kind": "del_stmt", "field": field, "pos": pos,
                                  "where": f"L{getattr(st, 'lineno', 0)}: 删除语句"})
    return cands


def _apply(tree, cand) -> None:
    node = next(n for i, n in enumerate(ast.walk(tree)) if i == cand["wi"])
    match cand["kind"]:
        case "cmp":
            node.ops[cand["j"]] = cand["new"]()
        case "bin" | "aug" | "bool":
            node.op = cand["new"]()
        case "const":
            node.value = cand["new"]
        case "ret_none":
            node.value = ast.Constant(value=None)
        case "invert":
            node.test = ast.UnaryOp(op=ast.Not(), operand=node.test)
        case "del_stmt":
            del getattr(node, cand["field"])[cand["pos"]]


def generate(source: str, limit: int = 60, seed: int = 0) -> list[Mutant]:
    """产出至多 limit 个可编译且与原码不同的变异体。"""
    tree = ast.parse(source)
    baseline = ast.unparse(tree)
    cands = _candidates(tree)
    random.Random(seed).shuffle(cands)

    out: list[Mutant] = []
    for cand in cands:
        if len(out) >= limit:
            break
        mutated = copy.deepcopy(tree)
        try:
            _apply(mutated, cand)
            ast.fix_missing_locations(mutated)
            src = ast.unparse(mutated)
            if src == baseline:
                continue              # 等价变异，不计入分母
            compile(src, "<mutant>", "exec")
        except (SyntaxError, ValueError, IndexError, AttributeError, StopIteration):
            continue                  # 改坏到编译不过的不算有效变异体
        out.append(Mutant(index=len(out), kind=cand["kind"], where=cand["where"], source=src))
    return out
