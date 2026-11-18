"""Command line entry point: ``python -m procsafe``.

Exit status: child status when it exits normally, 124 when the deadline
expired (mirroring timeout(1)), 128+signal when the child died by signal.
"""

from __future__ import annotations

import argparse
import json
import sys

from .runner import run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="procsafe",
        description="Run a command with a deadline, process-group kill and a bounded output window.",
    )
    parser.add_argument("--timeout", type=float, default=None, help="wall-clock budget in seconds")
    parser.add_argument("--grace", type=float, default=1.0, help="seconds between SIGTERM and SIGKILL")
    parser.add_argument(
        "--max-output",
        type=int,
        default=1 << 20,
        help="per-stream byte budget, split 75%% head / 25%% tail (default 1 MiB)",
    )
    parser.add_argument("--cwd", default=None, help="working directory for the child")
    parser.add_argument("--no-kill-orphans", action="store_true", help="leave group members alive after normal exit")
    parser.add_argument("--raw", action="store_true", help="print child stdout/stderr raw instead of JSON")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- command [args...]")
    args = parser.parse_args(argv)

    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("no command given; use: procsafe --timeout 2 -- sleep 10")

    result = run(
        command,
        timeout=args.timeout,
        grace=args.grace,
        max_output=args.max_output,
        cwd=args.cwd,
        kill_orphans=not args.no_kill_orphans,
    )

    if args.raw:
        sys.stdout.write(result.stdout if isinstance(result.stdout, str) else result.stdout.decode("utf-8", "replace"))
        sys.stderr.write(result.stderr if isinstance(result.stderr, str) else result.stderr.decode("utf-8", "replace"))
    else:
        json.dump(result.as_dict(), sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")

    if result.timed_out:
        return 124
    if result.returncode < 0:
        return 128 + (-result.returncode)
    return result.returncode


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())