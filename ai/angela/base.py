"""Shared run/step persistence helpers for the Angela pipeline.

Every phase (code/review/test/deploy/docs) records what it did via
:func:`log_step`, which writes an append-only ``AngelaStep`` row and is
what the live console feed renders. ``set_status`` advances the parent
``AngelaRun`` state machine.
"""

from __future__ import annotations

import logging

from ai.models import AngelaRun, AngelaStep


logger = logging.getLogger("plane.ai.angela")

# Steps store at most this many characters of detail — enough for a
# stack trace or a review rationale, bounded so a runaway test log
# can't bloat the table.
_DETAIL_CAP = 8000


def log_step(
    run: AngelaRun,
    *,
    phase: str,
    status: str,
    title: str = "",
    detail: str = "",
    iteration: int = 0,
) -> AngelaStep:
    """Append one step row to a run and return it."""
    if detail and len(detail) > _DETAIL_CAP:
        detail = detail[: _DETAIL_CAP - 20] + "\n…[truncated]"
    step = AngelaStep.objects.create(
        run=run,
        workspace_id=run.workspace_id,
        phase=phase,
        status=status,
        title=title[:200],
        detail=detail or "",
        iteration=iteration,
    )
    logger.info(
        "angela run=%s %s/%s it=%d %s",
        run.id,
        phase,
        status,
        iteration,
        title[:80],
    )
    return step


def set_status(run: AngelaRun, status: str, **fields) -> None:
    """Advance the run status and persist any extra column updates."""
    run.status = status
    for k, v in fields.items():
        setattr(run, k, v)
    update_fields = ["status", "updated_at", *fields.keys()]
    run.save(update_fields=update_fields)


def fail_run(run: AngelaRun, *, phase: str, error: str) -> None:
    """Terminal failure: log a failed step and flip the run to failed."""
    log_step(run, phase=phase, status=AngelaStep.STATUS_FAILED, title="failed", detail=error)
    set_status(run, AngelaRun.STATUS_FAILED, error=error[:4000])
