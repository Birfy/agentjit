"""The sandbox child process. **Must be self-contained** — it is started with
`python -I`, so its own directory is not on sys.path.

The design point is design.md §7.2: no holes in the sandbox. Generated code gets no I/O
primitives at all and `ctx` is empty. When capabilities are opened up, they go through a
facade RPC rather than by relaxing things here — otherwise the strength of the sandbox
becomes a function of how carefully each hole was cut, and holes only ever multiply.

Protocol: read one JSON job from stdin, write one JSON result to stdout.
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
    """`ctx` is empty: a pure function needs no capabilities.
    Capability injection (http / fs / tools) comes later."""

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
            pass  # unsupported on this platform; the parent's kill covers timeouts


# [KNOWN LIMITATION — do not try pre-warming again]
#
# Some stdlib functions import their implementation module on first call, which blows up
# here as `KeyError: '__import__'`. The one actually hit in practice is
# `datetime.datetime.strptime` (it needs `_strptime`) — the model's logic was entirely
# correct and this alone cost a wasted round of synthesis.
#
# **Importing `_strptime` into sys.modules first does not help**: strptime is implemented
# in C and goes through `PyImport_Import`, which looks `__import__` up in **the current
# globals' builtins**. Ours is the restricted dict, so it fails before sys.modules is
# ever consulted.
#
# Making it work means putting an `__import__` — even a restricted one — into
# SAFE_BUILTINS. That collides head-on with design.md §7.2 ("no holes in the sandbox")
# and is a bad trade: it saves one round of synthesis and wagers the whole sandbox
# boundary. So prompts.py **tells the model not to use it** instead.


def _build_globals(code_path):
    real = __builtins__ if isinstance(__builtins__, dict) else vars(__builtins__)
    safe = {n: real[n] for n in SAFE_BUILTINS if n in real}
    g = {"__builtins__": safe, "__name__": "generated", "__file__": code_path}
    for mod in INJECTED:
        g[mod] = __import__(mod)
    return g


def _jsonable(value):
    """Make sure the return value survives JSON. An unserialisable return is a failure —
    a result the caller cannot carry away is not a result."""
    blob = json.dumps(value, allow_nan=False, default=None)
    if len(blob) > MAX_VALUE_BYTES:
        raise ValueError(f"return value too large: {len(blob)} bytes > {MAX_VALUE_BYTES}")
    return json.loads(blob)


def main():
    job = json.load(sys.stdin)
    out_fd = os.dup(1)                      # grab the real stdout first
    os.close(1)
    os.open(os.devnull, os.O_WRONLY)        # anything the code prints goes nowhere
    sys.stdout = io.StringIO()
    sys.stderr = io.StringIO()

    # The CPU limit is only a backstop; the parent's wall clock is in charge. Set it
    # wider, so an ordinary timeout goes down the parent's path and the diagnosis reads
    # "timed out" rather than "killed by a signal".
    # RLIMIT_AS works on Linux; Darwin ignores it, and relies on the parent's watchdog.
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
            # json_report wants a path, not a file object. Write it next to the code so
            # it is destroyed with the temp directory.
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
