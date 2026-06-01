"""Isolated sandbox checkout management for Angela.

A :class:`Sandbox` is a per-run working copy of an allow-listed target
repo, living under ``ANGELA_WORKDIR/<run_id>``. All git + shell
operations are confined to that directory and time-boxed.

We deliberately keep this dependency-light: plain ``git`` and the
target's own test/deploy commands via ``subprocess``. No Docker-in-the
loop here — the *deployer* may shell out to a compose script, but the
sandbox itself is just a checkout.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import (
    AngelaConfigError,
    TargetConfig,
    assert_inside_sandbox,
    cmd_timeout,
    resolve_target,
    workdir,
)


# Env vars that would leak the HOST Plane app into the sandbox. They MUST
# be stripped before running the sandbox's own commands — otherwise, e.g.,
# pytest-django (installed in the Plane backend image) sees
# DJANGO_SETTINGS_MODULE and tries to bootstrap Plane's Django project
# from the sandbox cwd, which fails collection ("No module named 'plane'")
# and makes the target's tests impossible to pass. The sandbox must run
# as if it were a clean, unrelated checkout.
_STRIP_ENV_KEYS = (
    "DJANGO_SETTINGS_MODULE",
    "DJANGO_ALLOW_ASYNC_UNSAFE",
    "PYTHONPATH",
    "PYTHONSTARTUP",
    "PYTEST_PLUGINS",
    "PYTEST_ADDOPTS",
)


def _sandbox_env() -> dict[str, str]:
    """A copy of the process env with host-Plane/Django contamination
    removed, so the sandbox's test/build commands run in isolation."""
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV_KEYS}
    # Disable pytest's cache writes (sandbox is wiped each run anyway) and
    # belt-and-suspenders against a stray pytest-django activation.
    env["PYTEST_ADDOPTS"] = "-p no:cacheprovider"
    return env


logger = logging.getLogger("plane.ai.angela.sandbox")


@dataclass
class CmdResult:
    cmd: str
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def tail(self, n: int = 4000) -> str:
        out = (self.stdout or "") + (("\n--- stderr ---\n" + self.stderr) if self.stderr else "")
        return out[-n:]


class Sandbox:
    """A confined working copy for one Angela run."""

    def __init__(self, run_id: str, target: TargetConfig) -> None:
        self.run_id = str(run_id)
        self.target = target
        # One directory per run, asserted inside the sandbox root.
        self.root: Path = assert_inside_sandbox(self.run_id)

    # --- lifecycle --------------------------------------------------

    def prepare(self) -> None:
        """Make a fresh per-run working tree: clone the target repo, or —
        when the target has no ``clone_url`` (a "blank" scaffold) — init an
        empty git repo so the model builds from a clean slate.

        If the directory already exists (re-run), it is wiped first.
        """
        if self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)
        self.root.parent.mkdir(parents=True, exist_ok=True)

        if not self.target.clone_url:
            # Blank scaffold: empty repo + a minimal README so there's an
            # initial commit and tree to branch from.
            self.root.mkdir(parents=True, exist_ok=True)
            self._git("init", "-b", self.target.default_branch or "main")
            self._set_identity()
            (self.root / "README.md").write_text(
                "# Project scaffold\n\nBuilt by Angela.\n", encoding="utf-8"
            )
            self._git("add", "-A")
            self._git("commit", "-m", "scaffold", allow_fail=True)
            return

        res = self._run_raw(
            ["git", "clone", "--depth", "1", "--branch", self.target.default_branch,
             self.target.clone_url, str(self.root)],
            cwd=str(workdir()),
        )
        if not res.ok:
            # branch may not exist with that name; retry without --branch
            res = self._run_raw(
                ["git", "clone", "--depth", "1", self.target.clone_url, str(self.root)],
                cwd=str(workdir()),
            )
        if not res.ok:
            raise AngelaConfigError(f"clone failed: {res.tail()}")
        self._set_identity()

    def _set_identity(self) -> None:
        # Local identity so commits don't fail on a bare container.
        self._git("config", "user.email", "angela@planeai.local")
        self._git("config", "user.name", "Angela (AI)")

    def cleanup(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)

    # --- git --------------------------------------------------------

    def create_branch(self, name: str) -> str:
        safe = "".join(c if c.isalnum() or c in "-_/." else "-" for c in name)[:120]
        self._git("checkout", "-B", safe)
        return safe

    def write_file(self, rel_path: str, content: str) -> Path:
        """Write a file inside the checkout, guarding against traversal."""
        target = (self.root / rel_path).resolve()
        # Must stay inside this run's checkout.
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise AngelaConfigError(f"refusing to write outside checkout: {rel_path}") from exc
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def read_file(self, rel_path: str) -> str:
        target = (self.root / rel_path).resolve()
        target.relative_to(self.root)
        return target.read_text(encoding="utf-8", errors="replace")

    def seed_from(self, src: Path) -> int:
        """Copy a previous run's artifact files into this fresh checkout
        (refinement). Returns how many files were copied; skips VCS junk."""
        src = Path(src)
        if not src.exists():
            return 0
        count = 0
        for item in src.rglob("*"):
            if any(part in (".git", "__pycache__", ".pytest_cache") for part in item.parts):
                continue
            if item.is_file():
                dest = self.root / item.relative_to(src)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, dest)
                count += 1
        return count

    def snapshot(self, *, max_total: int = 40000, max_files: int = 30) -> dict[str, str]:
        """Read the текущие text files (path → content) for the coder to
        edit during a refinement. Bounded so a big project can't blow the
        prompt; oversized files are elided."""
        out: dict[str, str] = {}
        total = 0
        for rel in self.file_tree():
            if rel == "README.md":
                continue
            p = self.root / rel
            if not p.is_file():
                continue
            try:
                txt = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if total + len(txt) > max_total:
                out[rel] = "<...содержимое опущено из-за размера...>"
                continue
            out[rel] = txt
            total += len(txt)
            if len(out) >= max_files:
                break
        return out

    def stage_all(self) -> None:
        self._git("add", "-A")

    def commit(self, message: str) -> CmdResult:
        return self._git("commit", "-m", message, allow_fail=True)

    def diff(self, staged: bool = True) -> str:
        args = ["diff", "--staged"] if staged else ["diff"]
        return self._git(*args, allow_fail=True).stdout

    def file_tree(self, max_entries: int = 400) -> list[str]:
        """List tracked files (plus untracked, excluding ignored) so the
        coder has repo context without us shipping every byte."""
        res = self._git("ls-files", "--cached", "--others", "--exclude-standard", allow_fail=True)
        files = [l for l in res.stdout.splitlines() if l.strip()]
        return files[:max_entries]

    # --- arbitrary commands (test / install / deploy) ---------------

    def run_shell(self, command: str, *, extra_env: dict[str, str] | None = None) -> CmdResult:
        """Run a shell command inside the checkout (test/install/deploy).

        ``extra_env`` is merged on top of the de-contaminated sandbox env —
        used by the deployer to expose ANGELA_RUN_ID / ANGELA_PUBLIC_URL /
        ANGELA_ARTIFACT_DIR to custom deploy scripts.
        """
        if not command.strip():
            return CmdResult(command, 0, "", "")
        return self._run_raw(["/bin/sh", "-c", command], cwd=str(self.root), extra_env=extra_env)

    # --- internals --------------------------------------------------

    def _git(self, *args: str, allow_fail: bool = False) -> CmdResult:
        res = self._run_raw(["git", *args], cwd=str(self.root))
        if not res.ok and not allow_fail:
            logger.warning("git %s failed: %s", " ".join(args), res.tail(500))
        return res

    def _run_raw(
        self, argv: list[str], *, cwd: str, extra_env: dict[str, str] | None = None
    ) -> CmdResult:
        env = _sandbox_env()
        if extra_env:
            env.update({k: str(v) for k, v in extra_env.items()})
        try:
            proc = subprocess.run(
                argv,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=cmd_timeout(),
                env=env,
            )
            return CmdResult(
                cmd=" ".join(argv),
                returncode=proc.returncode,
                stdout=proc.stdout or "",
                stderr=proc.stderr or "",
            )
        except subprocess.TimeoutExpired as exc:
            return CmdResult(
                cmd=" ".join(argv),
                returncode=124,
                stdout=(exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
                stderr=f"timeout after {cmd_timeout()}s",
            )
        except FileNotFoundError as exc:
            return CmdResult(cmd=" ".join(argv), returncode=127, stdout="", stderr=str(exc))


def open_sandbox(run_id: str, target_key: str | None) -> Sandbox:
    """Resolve a target key and return a prepared :class:`Sandbox`."""
    target = resolve_target(target_key)
    sb = Sandbox(run_id, target)
    sb.prepare()
    return sb
