"""Disable Plane's zxcvbn-based password strength check.

Plane's auth adapter (plane.authentication.adapter.base.Authentication.
validate_password) calls ``zxcvbn(self.code)`` and raises
``PASSWORD_TOO_WEAK`` if score < 3. The same check sits in three
password-management views. All of them do ``from zxcvbn import zxcvbn``
at module scope, so the function reference is captured into each
module's namespace at import time.

To make any password pass we:

  1. Replace ``zxcvbn.zxcvbn`` in the source module so later imports
     get the no-op (defence for late-loaded modules).
  2. Walk ``sys.modules`` and overwrite the already-captured
     ``zxcvbn`` symbol in every plane.authentication.* module that
     imported it (defence for already-loaded modules).

The replacement returns the maximum score so any conditional like
``score < 3`` is False. No password length or complexity is enforced.

This is intentionally a one-way relaxation: there is no flag to
re-enable it because once removed the validators don't come back
until a fresh process boots without this overlay.
"""

from __future__ import annotations

import logging
import sys


logger = logging.getLogger("planeai.runtime.permissive_password")


def _fake_zxcvbn(password, user_inputs=None):  # noqa: D401 — drop-in shape
    """Always-passes replacement for zxcvbn.zxcvbn.

    Returns the same shape Plane reads (``score`` + ``feedback``).
    score=4 is zxcvbn's strongest tier so any ``score < N`` check
    short-circuits to False.
    """
    return {
        "password": password,
        "score": 4,
        "guesses": 1e12,
        "guesses_log10": 12,
        "calc_time": 0,
        "feedback": {"warning": "", "suggestions": []},
        "sequence": [],
        "crack_times_seconds": {},
        "crack_times_display": {},
    }


_TARGET_MODULES = (
    "plane.authentication.adapter.base",
    "plane.authentication.views.common",
    "plane.authentication.views.space.password_management",
    "plane.authentication.views.app.password_management",
)


def install() -> None:
    """Idempotent — calling multiple times is safe."""
    # Source-level patch: any module that hasn't imported zxcvbn yet
    # will pick up our replacement.
    try:
        import zxcvbn as _z
        _z.zxcvbn = _fake_zxcvbn
    except Exception as exc:  # pragma: no cover
        logger.warning("could not patch zxcvbn module: %s", exc)

    # Already-imported modules captured the original reference into
    # their own namespace. Overwrite each one.
    patched = []
    for name in _TARGET_MODULES:
        mod = sys.modules.get(name)
        if mod is None:
            continue
        if getattr(mod, "zxcvbn", None) is _fake_zxcvbn:
            continue
        if hasattr(mod, "zxcvbn"):
            mod.zxcvbn = _fake_zxcvbn
            patched.append(name)
    if patched:
        logger.info("permissive password: patched %s", ", ".join(patched))
    else:
        logger.info("permissive password: source-level patch installed")
