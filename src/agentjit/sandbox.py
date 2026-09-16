"""Sandboxed execution, parent side.

A subprocess plus restricted builtins plus rlimit. That is **enough to contain a bug,
not enough to contain an attacker** — this is a correctness sandbox, not a security
one. Real hardening (container / seccomp / microVM) is later work; the `python`
parameter is the seam where a different backend goes. See docs/design.md, open
question 3.
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_CHILD = Path(__file__).with_name("_child.py")


@dataclass
class CallResult:
    ok: bool
    value: Any = None
    error: str = ""
    tb: str = ""


@dataclass
class RunResult:
    ok: bool                                  # did the process itself finish normally
    results: list[CallResult] = field(default_factory=list)
    coverage: dict[str, Any] | None = None
    load_error: str = ""                      # the code failed to load or compile
    timed_out: bool = False
    killed: str = ""                          # "timeout" | "memory" | "signal:SIGxxx"
    wall_ms: float = 0.0

    @property
    def all_ok(self) -> bool:
        return self.ok and not self.load_error and all(r.ok for r in self.results)

    @property
    def why_dead(self) -> str:
        if self.killed == "timeout":
            return "timed out"
        if self.killed == "memory":
            return "killed: over the memory limit"
        if self.killed:
            return f"killed by a signal ({self.killed.split(':')[-1]})"
        if self.load_error:
            return "failed to load: " + self.load_error.strip().splitlines()[-1][:200]
        return ""


def _rss_kb(pid: int) -> int:
    """Read the child's resident memory: /proc on Linux (cheap), `ps` elsewhere."""
    try:
        with open(f"/proc/{pid}/statm") as fh:
            return int(fh.read().split()[1]) * (os.sysconf("SC_PAGE_SIZE") // 1024)
    except OSError:
        pass
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=1).stdout.strip()
        return int(out) if out else 0
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0


class _MemoryWatchdog(threading.Thread):
    """macOS ignores RLIMIT_AS outright, so the memory ceiling has to be watched here.

    It has to be watched: a single slip of the finger in generated code — `range(10**9)`
    — is enough to bring a development machine to its knees, and verification runs
    several sandboxes concurrently.
    """

    def __init__(self, proc: subprocess.Popen, mem_mb: int, interval: float = 0.1):
        super().__init__(daemon=True)
        self.proc, self.limit_kb, self.interval = proc, mem_mb * 1024, interval
        self.tripped = False
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.wait(self.interval):
            if self.proc.poll() is not None:
                return
            if _rss_kb(self.proc.pid) > self.limit_kb:
                self.tripped = True
                self.proc.kill()
                return

    def stop(self) -> None:
        self._stop.set()


class Sandbox:
    def __init__(self, python: str = sys.executable):
        self.python = python

    def run(
        self,
        source: str,
        entry: str,
        calls: list[dict[str, Any]],
        *,
        coverage: bool = False,
        timeout_ms: int = 5000,
        mem_mb: int = 512,
    ) -> RunResult:
        t0 = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="agentjit-") as td:
            code_path = Path(td) / "generated.py"
            code_path.write_text(source)
            job = json.dumps({
                "code_path": str(code_path),
                "entry": entry,
                "calls": calls,
                "coverage": coverage,
                "timeout_ms": timeout_ms,
                "mem_mb": mem_mb,
            })
            # -I: isolated mode. Ignores PYTHON* environment variables, skips the user
            # site directory, and keeps the script's own directory off sys.path — which
            # is why _child.py has to be self-contained.
            proc = subprocess.Popen(
                [self.python, "-I", str(_CHILD)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True,
            )
            dog = _MemoryWatchdog(proc, mem_mb)
            dog.start()
            timed_out = False
            try:
                out, err = proc.communicate(job, timeout=timeout_ms / 1000 + 1.5)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, err = proc.communicate()
                timed_out = True
            finally:
                dog.stop()

        wall = (time.perf_counter() - t0) * 1000
        if timed_out:
            return RunResult(ok=False, timed_out=True, killed="timeout", wall_ms=wall)
        if dog.tripped:
            return RunResult(ok=False, killed="memory", wall_ms=wall,
                             load_error=f"resident memory went over {mem_mb}MB")
        if proc.returncode and proc.returncode < 0:
            name = signal.Signals(-proc.returncode).name
            return RunResult(ok=False, killed=f"signal:{name}", wall_ms=wall,
                             load_error=f"the child was killed by {name}\n{err[-1000:]}")
        if proc.returncode != 0 or not out.strip():
            return RunResult(ok=False, wall_ms=wall,
                             load_error=(err or "the child produced no output")[-2000:])
        try:
            payload = json.loads(out)
        except json.JSONDecodeError:
            return RunResult(ok=False, wall_ms=wall,
                             load_error=f"the child's output was not JSON: {out[:500]!r}")

        return RunResult(
            ok=payload["ok"],
            results=[CallResult(**r) if r["ok"] else
                     CallResult(ok=False, error=r.get("error", ""), tb=r.get("tb", ""))
                     for r in payload["results"]],
            coverage=payload.get("coverage"),
            load_error=payload.get("load_error") or "",
            wall_ms=wall,
        )

    def run_many(self, jobs: list[dict[str, Any]], workers: int = 8) -> list[RunResult]:
        """Run several jobs concurrently."""
        with cf.ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(lambda j: self.run(**j), jobs))
