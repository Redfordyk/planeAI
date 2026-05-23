"""TZ 6.5 — the un-kill switch.

Counterpart to :mod:`ai.management.commands.disable_ai`. Same
options, opposite outcome. Implemented as a sibling Command that
subclasses :class:`_ToggleBase` so the two can never drift in
option parsing or in workspace resolution.

After a rollback, you re-enable per-workspace (or globally) once
the fix is deployed. The audit trail (operator + timestamp) lives
in ``logger.warning`` of plane.ai.rollback and in
``WorkspaceAIConfig.updated_at``.

Usage::

    python manage.py enable_ai --workspace <uuid-or-slug>
    python manage.py enable_ai --all-workspaces --confirm
"""

from __future__ import annotations

from ai.management.commands.disable_ai import _ToggleBase


class Command(_ToggleBase):
    """``python manage.py enable_ai ...`` — flip enabled → True."""

    help = (
        "Re-enable the AI layer after a rollback. Mirror of disable_ai. "
        "Use after deploying the fix that prompted the kill switch."
    )
    TARGET_ENABLED = True
