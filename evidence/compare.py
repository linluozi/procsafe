"""Measure what stdlib subprocess.run(timeout=) does versus procsafe.

Every number printed/written here comes from a real execution on this host.
Run: python3 evidence/compare.py > evidence/results.json
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from procsafe import group_members, run  # noqa: E402

PY = sys.executable


def pids_with(pattern: str) -> list[int]:
    """Live PIDs whose /proc/<pid>/cmdline contains pattern."""
    out = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as fh:
                cmd = fh.read().replace(b"\x00", b" ")
            with open(f"/proc/{entry}/stat", "rb") as fh:
                stat = fh.read()
        except OSError:
            continue
        if stat[stat.rfind(b")") + 2 :].startswith(b"Z"):
            continue
        if pattern.encode() in cmd:
            out.append(int(entry))
    return out


def cleanup(pattern: str) -> None:
    subprocess.run(["pkill", "-9", "-f", pattern], capture_output=True)


def scenario_orphan_holds_pipe(runs: int = 2) -> dict:
    """A grandchild inherits the stdout pipe. How long does each runner take?"""
    samples = []
    for i in range(runs):
        m = f"procsafe-orphan-pipe-{os.getpid()}-{i}-{time.time_ns()}"
        script = f"{PY} -c 'import time; time.sleep(8)' {m} & wait"
        t0 = time.perf_counter()
        try:
            subprocess.run(["sh", "-c", script], timeout=0.5, capture_output=True)
        except subprocess.TimeoutExpired:
            pass
        std_elapsed = time.perf_counter() - t0
        cleanup(m)

        t0 = time.perf_counter()
        run(["sh", "-c", script], timeout=0.5, grace=0.5)
        our_elapsed = time.perf_counter() - t0
        cleanup(m)
        samples.append({"stdlib_s": round(std_elapsed, 3), "procsafe_s": round(our_elapsed, 3)})
    return {"scenario": "grandchild inherits stdout pipe; deadline 0.5s", "samples": samples}


def scenario_orphan_survives(runs: int = 2) -> dict:
    """Grandchild redirects its own stdout, so stdlib returns but leaves it alive."""
    samples = []
    for i in range(runs):
        m = f"procsafe-orphan-alive-{os.getpid()}-{i}-{time.time_ns()}"
        script = f"{PY} -c 'import time; time.sleep(6)' {m} >/dev/null 2>&1 & wait"
        try:
            subprocess.run(["sh", "-c", script], timeout=0.5, capture_output=True)
        except subprocess.TimeoutExpired:
            pass
        time.sleep(0.2)
        std_alive = len(pids_with(m))
        cleanup(m)

        run(["sh", "-c", script], timeout=0.5, grace=0.3)
        time.sleep(0.2)
        our_alive = len(pids_with(m))
        cleanup(m)
        samples.append({"stdlib_survivors": std_alive, "procsafe_survivors": our_alive})
    return {"scenario": "grandchild outlives the direct child; deadline 0.5s", "samples": samples}


_MEM_WRAPPER = r"""
import json, resource, subprocess, sys, time
mode = sys.argv[1]
t0 = time.perf_counter()
child = [sys.executable, "-c", "import sys; sys.stdout.write('x' * 100_000_000)"]
if mode == "stdlib":
    p = subprocess.run(child, capture_output=True)
    kept = len(p.stdout)
else:
    sys.path.insert(0, sys.argv[2])
    from procsafe import run
    r = run(child, max_output=1 << 20)
    kept = len(r.stdout)
print(json.dumps({"elapsed_s": round(time.perf_counter() - t0, 3),
                  "parent_maxrss_kb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                  "kept_bytes": kept}))
"""


def scenario_memory(runs: int = 2) -> dict:
    samples = []
    for _ in range(runs):
        row = {}
        for mode in ("stdlib", "procsafe"):
            p = subprocess.run([PY, "-c", _MEM_WRAPPER, mode, ROOT], capture_output=True, text=True)
            row[mode] = json.loads(p.stdout)
        samples.append(row)
    return {"scenario": "child writes 100 MB to stdout", "samples": samples}


def main() -> int:
    data = {
        "host": {
            "python": sys.version.split()[0],
            "cpu": subprocess.run(["sh", "-c", "lscpu | sed -n 's/^Model name: *//p' | head -1"], capture_output=True, text=True).stdout.strip(),
            "kernel": os.uname().release,
        },
        "orphan_pipe": scenario_orphan_holds_pipe(),
        "orphan_alive": scenario_orphan_survives(),
        "memory": scenario_memory(),
    }
    json.dump(data, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())