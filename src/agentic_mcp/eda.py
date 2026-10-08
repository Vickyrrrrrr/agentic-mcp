"""The only way this server runs external programs.

argv-only (never shell), workspace jail, timeouts, output caps, binary
allowlist. Outcomes are returned, never raised - a missing tool is a
verdict, not a crash.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

ALLOWLIST = frozenset(
    {
        "yosys",
        "iverilog",
        "vvp",
        "verilator",
        "sby",
        "opensta",
        "openroad",
        "magic",
        "klayout",
        "netgen",
        "gtkwave",
        "volare",
        "ngspice",
        "fault",
    }
)

DEFAULT_TIMEOUT = 600
DEFAULT_MAX_OUTPUT = 256_000


@dataclass
class ExecResult:
    ok: bool
    code: int | None
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False
    timed_out: bool = False
    cancelled: bool = False
    refused: str = ""
    missing_tool: str = ""


def _refuse(reason: str) -> ExecResult:
    return ExecResult(ok=False, code=None, refused=reason)


def _cap(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + f"\n[truncated: output exceeded {limit} chars]", True


def which(tool: str) -> str | None:
    return shutil.which(tool)


def tool_status() -> dict[str, dict[str, str | bool]]:
    """Discovery probe: which EDA binaries exist. Read-only, no execution."""
    return {
        t: {"available": which(t) is not None, "path": which(t) or ""} for t in sorted(ALLOWLIST)
    }


def run(
    argv: list[str],
    workdir: str,
    timeout: int = DEFAULT_TIMEOUT,
    max_output: int = DEFAULT_MAX_OUTPUT,
    cancel: threading.Event | None = None,
    on_proc: Callable[[subprocess.Popen], None] | None = None,
) -> ExecResult:
    """Run an allowlisted binary with cwd jailed inside workdir.

    Popen-based (never shell): output is drained on a thread so a chatty
    tool cannot deadlock a full pipe, and `cancel` cooperatively kills the
    process. `on_proc` receives the live handle for external termination.
    """
    if not argv or not all(isinstance(p, str) and p for p in argv):
        return _refuse("refusing empty or non-string command")
    if argv[0] not in ALLOWLIST:
        return _refuse(f"refusing {argv[0]!r}: not in EDA allowlist")
    binary = which(argv[0])
    if binary is None:
        return ExecResult(
            ok=False, code=None, missing_tool=argv[0], stderr=f"{argv[0]} is not installed"
        )
    try:
        root = os.path.realpath(os.path.abspath(workdir))
    except Exception:
        return _refuse(f"refusing unresolvable workdir: {workdir}")
    if not os.path.isdir(root):
        return _refuse(f"refusing missing workdir: {workdir}")
    try:
        proc = subprocess.Popen(
            [binary, *argv[1:]],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=root,
        )
    except OSError as exc:
        return ExecResult(ok=False, code=None, stderr=f"failed to start: {exc}")
    if on_proc is not None:
        try:
            on_proc(proc)
        except Exception:
            pass
    collected: list = [None]

    def _drain() -> None:
        try:
            collected[0] = proc.communicate()
        except Exception:
            collected[0] = ("", "")

    drain = threading.Thread(target=_drain, daemon=True)
    drain.start()
    limit = max(int(timeout), 1)
    elapsed = 0.0
    step = 0.2
    while drain.is_alive():
        if cancel is not None and cancel.is_set():
            proc.kill()
            drain.join(timeout=10)
            logger.warning("tool cancelled: %s", argv[0])
            return ExecResult(
                ok=False, code=None, cancelled=True, stderr=f"{argv[0]} cancelled by user"
            )
        drain.join(timeout=step)
        elapsed += step
        if elapsed >= limit and drain.is_alive():
            proc.kill()
            drain.join(timeout=10)
            logger.warning("tool timeout: %s after %ss", argv[0], limit)
            return ExecResult(
                ok=False, code=None, timed_out=True, stderr=f"{argv[0]} timed out after {limit}s"
            )
    out, err = collected[0] or ("", "")
    half = max(int(max_output) // 2, 1024)
    out, t1 = _cap(out or "", half)
    err, t2 = _cap(err or "", half)
    code = proc.returncode
    return ExecResult(ok=code == 0, code=code, stdout=out, stderr=err, truncated=t1 or t2)
