import json
import os
import shutil
import subprocess
import sys
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from procsafe import group_members, run  # noqa: E402

PY = sys.executable


def marker() -> str:
    return f"procsafe-test-{os.getpid()}-{time.time_ns()}"


def alive(pattern: str) -> list[str]:
    """PIDs whose command line contains ``pattern`` (pgrep -f)."""
    if shutil.which("pgrep") is None:  # pragma: no cover
        return []
    proc = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
    return [line for line in proc.stdout.split() if line.strip()]


class BasicExecution(unittest.TestCase):
    def test_echo_returncode(self):
        r = run(["sh", "-c", "echo hello; exit 3"])
        self.assertEqual(r.returncode, 3)
        self.assertEqual(r.stdout, "hello\n")
        self.assertFalse(r.timed_out)
        self.assertFalse(r.stdout_truncated)

    def test_captures_both_streams_separately(self):
        r = run(["sh", "-c", "echo out; echo err >&2"])
        self.assertEqual(r.stdout, "out\n")
        self.assertEqual(r.stderr, "err\n")
        self.assertEqual(r.stdout_bytes, 4)
        self.assertEqual(r.stderr_bytes, 4)

    def test_stdin_is_devnull(self):
        r = run([PY, "-c", "import sys; print(repr(sys.stdin.read()))"])
        self.assertEqual(r.stdout, "''\n")

    def test_env_and_cwd(self):
        r = run([PY, "-c", "import os; print(os.environ['PROCSAFE_X'], os.getcwd())"], env={"PROCSAFE_X": "v1"}, cwd="/tmp")
        self.assertEqual(r.stdout, f"v1 /tmp\n")

    def test_signal_name(self):
        r = run([PY, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"])
        self.assertEqual(r.returncode, -15)
        self.assertEqual(r.signal_name, "SIGTERM")

    def test_binary_mode(self):
        r = run([PY, "-c", "import sys; sys.stdout.buffer.write(b'\\x00\\xff')"], text=False)
        self.assertEqual(r.stdout, b"\x00\xff")

    def test_no_false_truncation(self):
        r = run(["sh", "-c", "printf abc"])
        self.assertFalse(r.stdout_truncated)
        self.assertEqual(r.stdout, "abc")


class Deadlines(unittest.TestCase):
    def test_timeout_kills_sleep(self):
        t0 = time.perf_counter()
        r = run([PY, "-c", "import time; time.sleep(60)"], timeout=0.4, grace=0.5)
        elapsed = time.perf_counter() - t0
        self.assertTrue(r.timed_out)
        self.assertLess(elapsed, 3.0)
        self.assertIn(r.returncode, (-9, -15))
        self.assertIsNotNone(r.killed_signal)

    def test_sigterm_ignored_escalates_to_sigkill(self):
        code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"
        t0 = time.perf_counter()
        r = run([PY, "-c", code], timeout=0.4, grace=0.6)
        elapsed = time.perf_counter() - t0
        self.assertTrue(r.timed_out)
        self.assertEqual(r.returncode, -9)
        self.assertEqual(r.killed_signal, "SIGKILL")
        self.assertGreaterEqual(elapsed, 0.9)
        self.assertLess(elapsed, 3.5)

    def test_grandchild_does_not_survive_timeout(self):
        m = marker()
        script = f"{PY} -c 'import time; time.sleep(60)' {m} & wait"
        try:
            r = run(["sh", "-c", script], timeout=0.5, grace=0.5)
            self.assertTrue(r.timed_out)
            time.sleep(0.2)
            self.assertEqual(alive(m), [], "grandchild survived the group kill")
        finally:
            subprocess.run(["pkill", "-9", "-f", m], capture_output=True)

    def test_orphan_holding_pipe_does_not_hang(self):
        m = marker()
        script = f"{PY} -c 'import time; time.sleep(60)' {m} & exit 0"
        try:
            t0 = time.perf_counter()
            r = run(["sh", "-c", script], timeout=10, grace=0.3, drain=0.4)
            elapsed = time.perf_counter() - t0
            self.assertLess(elapsed, 2.5, "runner blocked on a pipe held by an orphan")
            self.assertEqual(r.returncode, 0)
            time.sleep(0.2)
            self.assertEqual(alive(m), [], "orphan survived after normal child exit")
        finally:
            subprocess.run(["pkill", "-9", "-f", m], capture_output=True)

    def test_timeout_none_runs_to_completion(self):
        r = run([PY, "-c", "import time; time.sleep(0.1); print('done')"], timeout=None)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, "done\n")


class OutputBudget(unittest.TestCase):
    def test_cap_keeps_head_and_tail(self):
        code = "import sys; sys.stdout.write('A'*1500000 + 'Z'*500000)"
        r = run([PY, "-c", code], max_output=65536)
        self.assertEqual(r.stdout_bytes, 2_000_000)
        self.assertTrue(r.stdout_truncated)
        self.assertEqual(len(r.stdout), 65536)
        self.assertEqual(r.stdout[:16], "A" * 16)
        self.assertEqual(r.stdout[-16:], "Z" * 16)

    def test_both_pipes_flood_without_deadlock(self):
        code = (
            "import sys\n"
            "for _ in range(40):\n"
            "    sys.stdout.write('o'*25000); sys.stdout.flush()\n"
            "    sys.stderr.write('e'*25000); sys.stderr.flush()\n"
        )
        r = run([PY, "-c", code], max_output=131072, timeout=20)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout_bytes, 1_000_000)
        self.assertEqual(r.stderr_bytes, 1_000_000)
        self.assertTrue(r.stdout_truncated and r.stderr_truncated)
        self.assertLessEqual(len(r.stdout), 131072)
        self.assertLessEqual(len(r.stderr), 131072)

    def test_stderr_budget_independent(self):
        code = "import sys; sys.stderr.write('E'*100000); print('ok')"
        r = run([PY, "-c", code], max_output=4096)
        self.assertEqual(r.stdout, "ok\n")
        self.assertTrue(r.stderr_truncated)
        self.assertLessEqual(len(r.stderr), 4096)

    def test_invalid_arguments(self):
        with self.assertRaises(ValueError):
            run([], )
        with self.assertRaises(ValueError):
            run(["true"], timeout=0)
        with self.assertRaises(ValueError):
            run(["true"], max_output=1)


class CommandLine(unittest.TestCase):
    def _cli(self, *args):
        env = dict(os.environ)
        env["PYTHONPATH"] = ROOT
        return subprocess.run([PY, "-m", "procsafe", *args], cwd=ROOT, capture_output=True, text=True, env=env)

    def test_cli_json_and_exit_code(self):
        p = self._cli("--max-output", "4096", "--", "sh", "-c", "echo hi; exit 3")
        self.assertEqual(p.returncode, 3, p.stderr)
        doc = json.loads(p.stdout)
        self.assertEqual(doc["stdout"], "hi\n")
        self.assertEqual(doc["returncode"], 3)
        self.assertFalse(doc["timed_out"])

    def test_cli_timeout_exit_124(self):
        p = self._cli("--timeout", "0.3", "--grace", "0.3", "--", PY, "-c", "import time; time.sleep(30)")
        self.assertEqual(p.returncode, 124, p.stderr)
        self.assertTrue(json.loads(p.stdout)["timed_out"])

    def test_cli_requires_command(self):
        p = self._cli()
        self.assertEqual(p.returncode, 2)

    def test_group_members_helper(self):
        # our own process group is visible and contains us
        self.assertIn(os.getpid(), group_members(os.getpgid(0)))


if __name__ == "__main__":
    unittest.main(verbosity=2)