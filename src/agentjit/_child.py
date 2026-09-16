"""沙箱子进程。**必须自包含** —— 用 `python -I` 启动，脚本目录不在 sys.path 上。

设计要点见 docs/design.md §7.2：沙箱里一个洞都不开。生成的代码拿不到任何 I/O
原语，`ctx` 里也什么都没有（M0 只做纯函数）。要开放能力时，走 facade RPC 而不是
在这里放宽限制 —— 那样沙箱强度就取决于口子写得好不好，而口子只会越开越多。

协议：stdin 读一个 JSON job，stdout 写一个 JSON 结果。
"""
import io
import json
import os
import sys
import traceback

MAX_VALUE_BYTES = 1 << 20

SAFE_BUILTINS = (
    "abs all any ascii bin bool bytes callable chr dict divmod enumerate filter float "
    "format frozenset hasattr hash hex id int isinstance issubclass iter len list map "
    "max min next object oct ord pow range repr reversed round set slice sorted str sum "
    "tuple type zip True False None NotImplemented Ellipsis "
    "Exception BaseException ArithmeticError ArithmeticError AssertionError AttributeError "
    "EOFError FloatingPointError IndexError KeyError KeyboardInterrupt LookupError "
    "MemoryError NameError NotImplementedError OverflowError RecursionError RuntimeError "
    "StopIteration TypeError UnboundLocalError UnicodeDecodeError UnicodeEncodeError "
    "UnicodeError ValueError ZeroDivisionError"
).split()

INJECTED = ("math", "re", "json", "datetime", "decimal",
            "statistics", "itertools", "functools", "collections")


class Ctx:
    """M0 的 ctx 是空的 —— 纯函数不需要任何能力。
    能力注入（http / fs / tools / llm）是 M3。"""

    class _Log:
        def __call__(self, *a, **k):
            pass
        info = debug = warn = error = __call__

    log = _Log()


def _apply_limits(mem_mb, cpu_s):
    try:
        import resource
    except ImportError:
        return
    for res, val in (
        ("RLIMIT_AS", mem_mb * 1024 * 1024),
        ("RLIMIT_CPU", cpu_s),
        ("RLIMIT_NPROC", 0),
    ):
        limit = getattr(resource, res, None)
        if limit is None:
            continue
        try:
            resource.setrlimit(limit, (val, val))
        except (ValueError, OSError):
            pass  # 平台不支持就算了；超时由父进程的 kill 兜底


# 【已知限制，别再试预热了】
#
# 有些标准库函数在第一次调用时才去 import 它的实现模块，在这里会炸成
# `KeyError: '__import__'`。实测撞到的是 `datetime.datetime.strptime`
# （内部要 `_strptime`）—— 模型写的逻辑完全正确，被这个挡下来，白烧一轮合成。
#
# **先把 `_strptime` import 进 sys.modules 是没用的**：strptime 是 C 实现，
# 走 `PyImport_Import`，那个函数在**当前 globals 的 builtins** 里找 `__import__`，
# 而我们给的是受限字典 —— 在查 sys.modules 之前就已经失败了。
#
# 要让它能用，只能往 SAFE_BUILTINS 里放一个（哪怕是受限的）`__import__`。
# 那和 design.md §7.2"沙箱里一个洞都不开"直接冲突，不划算：省的只是一轮合成，
# 赌的是整个沙箱边界。所以改成在 prompts.py 里**告诉模型别用它**。


def _build_globals(code_path):
    real = __builtins__ if isinstance(__builtins__, dict) else vars(__builtins__)
    safe = {n: real[n] for n in SAFE_BUILTINS if n in real}
    g = {"__builtins__": safe, "__name__": "generated", "__file__": code_path}
    for mod in INJECTED:
        g[mod] = __import__(mod)
    return g


def _jsonable(value):
    """确保返回值能过 JSON。不能序列化的返回值等同于失败 ——
    一个 agent 拿不走的结果不算结果。"""
    blob = json.dumps(value, allow_nan=False, default=None)
    if len(blob) > MAX_VALUE_BYTES:
        raise ValueError(f"返回值过大: {len(blob)} 字节 > {MAX_VALUE_BYTES}")
    return json.loads(blob)


def main():
    job = json.load(sys.stdin)
    out_fd = os.dup(1)                      # 先抢下真正的 stdout
    os.close(1)
    os.open(os.devnull, os.O_WRONLY)        # 被测代码的任何输出都进黑洞
    sys.stdout = io.StringIO()
    sys.stderr = io.StringIO()

    # CPU 限额只是兜底：主控是父进程的墙钟计时器。设得比它宽，
    # 这样正常超时会走父进程那条路，诊断信息才说得清是"超时"而不是"被信号杀了"。
    # RLIMIT_AS 在 Linux 上有效，Darwin 直接忽略 —— 那边靠父进程的看门狗。
    _apply_limits(job.get("mem_mb", 512), job.get("timeout_ms", 5000) // 1000 + 6)

    code_path = job["code_path"]
    cov = None
    if job.get("coverage"):
        import coverage
        cov = coverage.Coverage(data_file=None, branch=True, include=[code_path])
        cov.start()

    payload = {"ok": True, "results": [], "coverage": None, "load_error": None}
    try:
        with open(code_path) as fh:
            source = fh.read()
        g = _build_globals(code_path)
        exec(compile(source, code_path, "exec"), g)
        fn = g[job["entry"]]
    except BaseException:
        if cov:
            cov.stop()
        payload["ok"] = False
        payload["load_error"] = traceback.format_exc(limit=6)
        _emit(out_fd, payload)
        return

    for params in job["calls"]:
        try:
            payload["results"].append({"ok": True, "value": _jsonable(fn(params, Ctx()))})
        except BaseException as e:
            payload["results"].append({
                "ok": False,
                "error": f"{type(e).__name__}: {e}",
                "tb": traceback.format_exc(limit=6),
            })

    if cov:
        cov.stop()
        try:
            # json_report 要的是路径，不是文件对象。写在代码旁边，随临时目录一起销毁。
            report_path = code_path + ".cov.json"
            cov.json_report(outfile=report_path)
            with open(report_path) as fh:
                report = json.load(fh)
            for path, data in report.get("files", {}).items():
                if os.path.samefile(path, code_path):
                    payload["coverage"] = {
                        "summary": data["summary"],
                        "missing_lines": data.get("missing_lines", []),
                        "missing_branches": data.get("missing_branches", []),
                    }
                    break
        except Exception as e:
            payload["coverage"] = {"error": f"{type(e).__name__}: {e}"}

    _emit(out_fd, payload)


def _emit(fd, payload):
    with os.fdopen(fd, "w") as fh:
        json.dump(payload, fh)


if __name__ == "__main__":
    main()
