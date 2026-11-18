"""Bounded, process-group-scoped subprocess execution.

The contract this module implements:

* the child runs in its own session/process group, so a timeout can be
  delivered to grandchildren as well;
* escalation is SIGTERM to the group, a grace period, then SIGKILL to the
  group;
* captured output is kept in a fixed-size head+tail window, so a child that
  prints gigabytes cannot grow the parent's memory;
* the parent never blocks forever on a pipe held open by an orphan.

Only the standard library is used.
"""

from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass, field
from typing import Mapping, Sequence

__all__ = ["Result", "run", "group_members"]

_CHUNK = 65536


@dataclass
class Result:
    argv: list[str]
    returncode: int
    timed_out: bool
    duration: float
    stdout: "str | bytes"
    stderr: "str | bytes"
    stdout_bytes: int
    stderr_bytes: int
    stdout_truncated: bool
    stderr_truncated: bool
    killed_signal: str | None = None
    group_killed: bool = False
    encoding: str = "utf-8"

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def signal_name(self) -> str | None:
        if self.returncode < 0:
            try:
                return signal.Signals(-self.returncode).name
            except ValueError:  # pragma: no cover - defensive
                return f"SIG{-self.returncode}"
        return None

    def as_dict(self) -> dict:
        return {
            "argv": self.argv,
            "returncode": self.returncode,
            "signal": self.signal_name,
            "timed_out": self.timed_out,
            "duration_s": round(self.duration, 6),
            "stdout_bytes": self.stdout_bytes,
            "stderr_bytes": self.stderr_bytes,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "killed_signal": self.killed_signal,
            "group_killed": self.group_killed,
            "stdout": self.stdout if isinstance(self.stdout, str) else self.stdout.decode("utf-8", "replace"),
            "stderr": self.stderr if isinstance(self.stderr, str) else self.stderr.decode("utf-8", "replace"),
        }


class _Window:
    """Head+tail byte window with a hard total budget."""

    __slots__ = ("head_cap", "tail_cap", "head", "tail", "total")

    def __init__(self, head_cap: int, tail_cap: int) -> None:
        self.head_cap = head_cap
        self.tail_cap = tail_cap
        self.head = bytearray()
        self.tail = bytearray()
        self.total = 0

    def feed(self, chunk: bytes) -> None:
        self.total += len(chunk)
        room = self.head_cap - len(self.head)
        if room > 0:
            take = chunk[:room]
            self.head += take
            chunk = chunk[room:]
        if chunk and self.tail_cap:
            self.tail += chunk
            overflow = len(self.tail) - self.tail_cap
            if overflow > 0:
                del self.tail[:overflow]

    @property
    def truncated(self) -> bool:
        return self.total > len(self.head) + len(self.tail)

    def value(self) -> bytes:
        return bytes(self.head) + bytes(self.tail)


def group_members(pgid: int) -> list[int]:
    """Return live PIDs whose process group is ``pgid`` (Linux /proc scan).

    Zombies are excluded.  Used by tests and by the evidence script; the
    runner itself does not need it.  Returns ``[]`` on non-Linux systems.
    """
    out: list[int] = []
    try:
        entries = os.listdir("/proc")
    except OSError:  # pragma: no cover - non-Linux
        return out
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", "rb") as fh:
                data = fh.read()
        except OSError:
            continue
        # comm may contain spaces/parens; fields after the last ')' are stable:
        # state ppid pgrp session ...
        tail = data[data.rfind(b")") + 2 :].split()
        if len(tail) < 3:
            continue
        state, pgrp = tail[0], tail[2]
        if state == b"Z":
            continue
        try:
            if int(pgrp) == pgid:
                out.append(int(entry))
        except ValueError:
            continue
    return out


def _kill_group(pgid: int, sig: int) -> bool:
    try:
        os.killpg(pgid, sig)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - same uid in practice
        return False


def run(
    argv: Sequence[str],
    *,
    timeout: float | None = None,
    grace: float = 1.0,
    max_output: int = 1 << 20,
    encoding: str = "utf-8",
    text: bool = True,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
    kill_orphans: bool = True,
    drain: float = 0.5,
) -> Result:
    """Run ``argv`` with a wall-clock deadline and a fixed output budget.

    ``timeout`` is the wall-clock budget for the whole process group.
    On expiry the group gets SIGTERM, then SIGKILL after ``grace`` seconds.
    ``max_output`` is the per-stream budget (bytes), split 75% head / 25% tail.
    ``kill_orphans`` also reaps group members that outlive a normally-exiting
    child (a daemon holding a pipe open would otherwise stall the drain).
    """
    if not argv:
        raise ValueError("argv must not be empty")
    if max_output < 2:
        raise ValueError("max_output must be >= 2")
    if timeout is not None and timeout <= 0:
        raise ValueError("timeout must be > 0 or None")
    if grace < 0 or drain < 0:
        raise ValueError("grace and drain must be >= 0")

    head_cap = max_output * 3 // 4
    tail_cap = max_output - head_cap
    argv = [str(a) for a in argv]

    child_env = None
    if env is not None:
        child_env = {**os.environ, **{str(k): str(v) for k, v in env.items()}}

    start = time.perf_counter()
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        env=child_env,
        cwd=cwd,
        close_fds=True,
    )
    pgid = proc.pid  # start_new_session=True makes the child a group leader

    out_win = _Window(head_cap, tail_cap)
    err_win = _Window(head_cap, tail_cap)
    sel = selectors.DefaultSelector()
    assert proc.stdout is not None and proc.stderr is not None
    sel.register(proc.stdout, selectors.EVENT_READ, out_win)
    sel.register(proc.stderr, selectors.EVENT_READ, err_win)

    deadline = None if timeout is None else start + timeout
    signaled_at = None
    hard_killed = False
    killed_signal: str | None = None
    group_killed = False
    exited_at: float | None = None

    try:
        while True:
            now = time.perf_counter()

            if deadline is not None and signaled_at is None and now >= deadline:
                signaled_at = now
                if _kill_group(pgid, signal.SIGTERM):
                    killed_signal = "SIGTERM"
                    group_killed = True
            elif signaled_at is not None and not hard_killed and now - signaled_at >= grace:
                if _kill_group(pgid, signal.SIGKILL):
                    killed_signal = "SIGKILL"
                    group_killed = True
                hard_killed = True

            if proc.poll() is not None and exited_at is None:
                exited_at = now
            if not sel.get_map():
                break
            if exited_at is not None and now - exited_at > drain:
                # Child is gone but a pipe is still open (orphan holds it).
                if kill_orphans:
                    if _kill_group(pgid, signal.SIGKILL):
                        group_killed = True
                        killed_signal = killed_signal or "SIGKILL"
                break

            wait = 0.02
            if deadline is not None and signaled_at is None:
                wait = max(0.0, min(wait, deadline - now))
            elif signaled_at is not None and not hard_killed:
                wait = max(0.0, min(wait, grace - (now - signaled_at)))
            events = sel.select(wait)
            for key, _ in events:
                fd = key.fileobj.fileno()
                try:
                    chunk = os.read(fd, _CHUNK)
                except OSError:
                    chunk = b""
                if not chunk:
                    sel.unregister(key.fileobj)
                else:
                    key.data.feed(chunk)
    finally:
        sel.close()
        if proc.poll() is None:
            _kill_group(pgid, signal.SIGKILL)
            group_killed = True
            killed_signal = killed_signal or "SIGKILL"
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            pass
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()

    duration = time.perf_counter() - start
    timed_out = signaled_at is not None

    if text:
        out = out_win.value().decode(encoding, "replace")
        err = err_win.value().decode(encoding, "replace")
    else:
        out = out_win.value()
        err = err_win.value()

    return Result(
        argv=argv,
        returncode=proc.returncode if proc.returncode is not None else -9,
        timed_out=timed_out,
        duration=duration,
        stdout=out,
        stderr=err,
        stdout_bytes=out_win.total,
        stderr_bytes=err_win.total,
        stdout_truncated=out_win.truncated,
        stderr_truncated=err_win.truncated,
        killed_signal=killed_signal,
        group_killed=group_killed,
        encoding=encoding,
    )