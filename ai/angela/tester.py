"""Run the sandbox repo's tests and parse the outcome.

"сама тестирует" — after self-review approves, Angela installs deps
(once) and runs the target's configured test command inside the
checkout. We return a structured result the pipeline branches on; the
raw tail is logged as a step so the console shows real output.

We intentionally do not try to be a universal test runner: the target
config declares ``test_cmd`` (and optional ``install_cmd``). Angela just
executes them and reads the exit code, with a light heuristic summary
of pytest-style output for the feed.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from .sandbox import CmdResult, Sandbox


logger = logging.getLogger("plane.ai.angela.tester")


@dataclass
class TestResult:
    passed: bool
    summary: str
    output_tail: str
    returncode: int


_PYTEST_SUMMARY = re.compile(
    r"(\d+)\s+passed|(\d+)\s+failed|(\d+)\s+error", re.IGNORECASE
)


def _summarize(res: CmdResult) -> str:
    """Pull a one-liner from common test-runner output."""
    text = (res.stdout or "") + "\n" + (res.stderr or "")
    # last line that looks like a pytest summary
    summary_lines = [
        l.strip()
        for l in text.splitlines()
        if _PYTEST_SUMMARY.search(l) or "passed" in l or "failed" in l
    ]
    if summary_lines:
        return summary_lines[-1][:200]
    if res.returncode == 124:
        return "timeout"
    return f"exit code {res.returncode}"


def run_tests(sandbox: Sandbox, *, install_cmd: str = "", test_cmd: str = "") -> TestResult:
    install_cmd = install_cmd or sandbox.target.install_cmd
    test_cmd = test_cmd or sandbox.target.test_cmd

    if install_cmd:
        inst = sandbox.run_shell(install_cmd)
        if not inst.ok:
            logger.info("angela tester: install failed (continuing): %s", inst.tail(300))

    if not test_cmd:
        return TestResult(True, "no test command configured (skipped)", "", 0)

    res = sandbox.run_shell(test_cmd)
    summary = _summarize(res)
    return TestResult(
        passed=res.ok,
        summary=summary,
        output_tail=res.tail(4000),
        returncode=res.returncode,
    )
