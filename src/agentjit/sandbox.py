"""沙箱执行（父进程侧）。

M0 用子进程 + 受限 builtins + rlimit。这**足以隔离错误，不足以隔离攻击者** ——
它是一个正确性沙箱，不是安全沙箱。真正的安全加固（容器 / seccomp / microVM）
是 M2，接口留了 `python` 参数可以换后端。见 docs/design.md 开放问题 3。
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
    ok: bool                                  # 进程层面是否正常完成
    results: list[CallResult] = field(default_factory=list)
    coverage: dict[str, Any] | None = None
    load_error: str = ""                      # 代码本身加载/编译失败
    timed_out: bool = False
    killed: str = ""                          # "timeout" | "memory" | "signal:SIGxxx"
    wall_ms: float = 0.0

    @property
    def all_ok(self) -> bool:
        return self.ok and not self.load_error and all(r.ok for r in self.results)

    @property
    def why_dead(self) -> str:
        if self.killed == "timeout":
            return "执行超时"
        if self.killed == "memory":
            return "内存超限被杀"
        if self.killed:
            return f"被信号杀死（{self.killed.split(':')[-1]}）"
        if self.load_error:
            return "加载失败: " + self.load_error.strip().splitlines()[-1][:200]
        return ""


def _rss_kb(pid: int) -> int:
    """读子进程常驻内存。Linux 走 /proc（便宜），其余平台退回 ps。"""
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
    """macOS 直接忽略 RLIMIT_AS，所以内存上限只能在父进程这边盯。

    不盯不行：变异测试会并发跑几十个沙箱，生成代码里一个手滑的 range(10**9)
    就足以把开发机拖死。
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
            # -I: 隔离模式。忽略 PYTHON* 环境变量、不加载用户 site、
            # 不把脚本目录放进 sys.path（所以 _child.py 必须自包含）。
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
                             load_error=f"常驻内存超过 {mem_mb}MB")
        if proc.returncode and proc.returncode < 0:
            name = signal.Signals(-proc.returncode).name
            return RunResult(ok=False, killed=f"signal:{name}", wall_ms=wall,
                             load_error=f"子进程被 {name} 杀死\n{err[-1000:]}")
        if proc.returncode != 0 or not out.strip():
            return RunResult(ok=False, wall_ms=wall,
                             load_error=(err or "子进程无输出")[-2000:])
        try:
            payload = json.loads(out)
        except json.JSONDecodeError:
            return RunResult(ok=False, wall_ms=wall,
                             load_error=f"子进程输出不是 JSON: {out[:500]!r}")

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
        """并发跑多个变体。变异测试要跑几十个变异体，串行会慢一个数量级。"""
        with cf.ThreadPoolExecutor(max_workers=workers) as pool:
            return list(pool.map(lambda j: self.run(**j), jobs))
