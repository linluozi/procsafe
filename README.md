# procsafe — a subprocess runner with a real deadline, group kill, and a fixed output budget

`subprocess.run(cmd, timeout=2)` sends **SIGKILL to the direct child only**. Anything that child
spawned keeps running, and `capture_output=True` buffers everything the child printed. `procsafe`
runs the command in its own session, so the deadline reaches the whole process group, and keeps
captured output in a fixed head+tail window instead of growing the parent's memory.

Upstream: none

```python
from procsafe import run

r = run(["bash", "-c", "make test"], timeout=30, max_output=1 << 20)
print(r.ok, r.returncode, r.timed_out, r.duration, r.stdout_truncated)
```

## Why

Agent runners, CI wrappers and evaluation harnesses all execute untrusted commands. Two failure
modes show up repeatedly:

1. **The deadline is a lie.** The child forks a worker; the timeout fires; the worker keeps the
   port, the file lock, or the GPU. Measured on this host: after `subprocess.run(..., timeout=0.5)`
   killed the shell, **1 of 1 grandchildren was still alive**.
2. **The output has no budget.** `capture_output=True` on a child that prints 100 MB costs the
   parent ~208 MB RSS. Measured here: **207,872 KB** peak RSS for stdlib vs **17,800 KB** for
   `procsafe` at a 1 MiB window — the same 100 MB of child output, 11.7x less parent memory, and
   95.4x fewer retained bytes.

## Install

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e .
procsafe --timeout 2 -- sh -c 'echo hello; sleep 30'
```

No runtime dependencies. Python 3.9+. POSIX only (uses `os.setsid` semantics and `os.killpg`).

## Command line

```bash
procsafe --timeout 5 --grace 1 --max-output 1048576 -- make test
```

Prints a JSON `Result` to stdout. Exit status is the child's status, `124` when the deadline
expired (same convention as `timeout(1)`), or `128+signal` when the child died by signal.
`--raw` prints the child's captured streams instead of JSON.

## The contract

| Behaviour | Implementation |
| --- | --- |
| Deadline covers grandchildren | child is a session/group leader (`start_new_session=True`); expiry sends `SIGTERM` to `os.killpg` |
| Escalation | after `grace` seconds (default 1.0) the whole group gets `SIGKILL` |
| Bounded memory | per-stream budget `max_output` bytes, split 75% head / 25% tail; totals and truncation flags are reported |
| No hang on an orphan | if the direct child exits but a pipe stays open, the runner drains for `drain` seconds, then kills the group |
| No shell | `argv` is executed directly; there is no implicit `shell=True` |
| stdin | `/dev/null`, so a child that reads stdin gets EOF instead of inheriting the caller's terminal |

`Result` fields: `returncode`, `signal_name`, `timed_out`, `duration`, `stdout`, `stderr`,
`stdout_bytes`, `stderr_bytes`, `stdout_truncated`, `stderr_truncated`, `killed_signal`,
`group_killed`.

## Measured evidence

All numbers below were produced by `evidence/compare.py` on this host; the raw samples are in
`evidence/results.json`.

Environment: CPython 3.12.3, AMD EPYC 7K62, Linux 6.8.0-124-generic, 3.6 GB RAM.

### 1. Grandchild survives a stdlib timeout

Command under test: `sh -c "<python> -c 'time.sleep(6)' MARKER >/dev/null 2>&1 & wait"`,
deadline 0.5 s. After the runner returned, live PIDs whose `/proc/<pid>/cmdline` contains `MARKER`
were counted.

| Runner | survivors (2 runs) |
| --- | --- |
| `subprocess.run(..., timeout=0.5)` | 1, 1 |
| `procsafe.run(..., timeout=0.5, grace=0.3)` | 0, 0 |

Reproduce:

```bash
python3 evidence/compare.py
```

### 2. Parent memory with a 100 MB child

Child: `python3 -c "sys.stdout.write('x' * 100_000_000)"`. Each runner was measured in a fresh
process with `resource.getrusage(RUSAGE_SELF).ru_maxrss`.

| Runner | peak parent RSS | bytes retained | wall time |
| --- | --- | --- | --- |
| `subprocess.run(capture_output=True)` | 207,872 KB | 100,000,000 | 0.29 s |
| `procsafe.run(max_output=1 MiB)` | 17,800 KB | 1,048,576 | 0.19 s |

The 0.19 s vs 0.29 s difference is a side effect of not accumulating 100 MB of bytes, not a
benchmark claim; it is reported because it was measured.

### 3. A pipe-holding grandchild does not stretch the deadline

Same shape but the grandchild inherits stdout: both runners returned at ~0.5 s (stdlib because it
calls `wait()` rather than reading the pipe after killing the child, `procsafe` because the group
kill closes the pipe). See `orphan_pipe` in `evidence/results.json`. This scenario is listed
because it did **not** produce a difference — the observable difference is in scenario 1.

## Tests

```bash
python3 -m unittest discover -s tests -t . -v
```

20 tests, **all passing in 3.48 s** on this host. The adversarial cases are the point:

- child ignores `SIGTERM` → escalated to `SIGKILL`, wall clock between `timeout+grace` and 3.5 s;
- grandchild sleeps 60 s → no matching PID survives the deadline;
- orphan holds the stdout pipe after a normal child exit → runner returns in < 2.5 s and reaps it;
- 2 MB stdout with `max_output=64 KiB` → exactly 64 KiB retained, first bytes `A`, last bytes `Z`,
  `stdout_bytes == 2_000_000`, `truncated == True`;
- 1 MB on stdout and 1 MB on stderr simultaneously → no deadlock, both budgets independent;
- CLI exit codes 3, 124 and usage error 2; binary mode; env/cwd; `stdin=/dev/null`.

## API

```python
run(argv, *, timeout=None, grace=1.0, max_output=1 << 20,
    encoding="utf-8", text=True, env=None, cwd=None,
    kill_orphans=True, drain=0.5) -> Result
```

- `timeout=None` waits indefinitely (still bounded output and orphan drain).
- `env` is merged into the parent environment, not substituted for it.
- `text=False` returns `bytes` instead of decoded `str`.
- `max_output` is per stream, so total retained bytes are at most `2 * max_output`.

## Limitations

- **Not a security sandbox.** It bounds time and captured bytes, not CPU, file writes, or network.
  Use a container or `resource.setrlimit` for hostile code.
- **`grace` is a deliberate trade-off**: a cooperative child exits on `SIGTERM` immediately;
  a child that ignores it costs up to `grace` extra seconds before `SIGKILL`.
- Truncation joins the head and tail at a byte boundary, so a multi-byte UTF-8 character can be
  split at the seam and decoded as `U+FFFD`.
- `kill_orphans=True` also kills deliberately daemonized grandchildren after a normal child exit.
  Pass `kill_orphans=False` if that is not what you want.
- No Windows support.

## License

MIT. Copyright (c) 2026 linluozi. Original work; no upstream code was copied.

## 本轮创新

把「超时」从对直接子进程的一次 `SIGKILL` 提升为**有回收契约的进程组生命周期**：deadline 覆盖孙进程、
SIGTERM→grace→SIGKILL 分级、输出头尾预算、孤儿持管道时不阻塞，并给出可复现的对照证据
（stdlib 超时后 1/1 孙进程存活；100 MB 输出下父进程 207,872 KB vs 17,800 KB）。
与已有 `timeout(1)`/`subprocess.run` 的差别在于：前者只杀直接子进程、后者无输出预算；
本仓库把四件事作为一个可测试的契约实现，而不是命令行包装。