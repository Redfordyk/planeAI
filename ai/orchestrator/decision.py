"""Decision Layer (TZ 7.2).

Heart of agent safety. Every action an agent wants to perform is
classified by :func:`decide` into one of four risk levels:

    AUTO      — perform immediately, log it.
    NOTIFY    — perform, but mark for user attention.
    CONFIRM   — DO NOT perform; create a proposed AgentAction the user
                must approve in the UI.
    ESCALATE  — DO NOT perform; create a comment on the goal/task for
                a human PM with options.

The matrix is intentionally permissive for reversible/cheap actions
(label, priority, comment) and conservative for irreversible/expensive
ones (massive plan, hire a freelancer). Deletion is HARD-NULL — the
function returns ``None`` and the caller must treat that as "agent is
NEVER allowed to do this, regardless of escalation". This matches the
CLAUDE.md invariant: agents never delete.

The matrix is overridable per-workspace via WorkspaceAIConfig in the
future, but right now it's hardcoded — adding to the matrix requires
a code change + review, which is the right friction for changing
agent autonomy.
"""

from __future__ import annotations

from typing import Optional

AUTO = "AUTO"
NOTIFY = "NOTIFY"
CONFIRM = "CONFIRM"
ESCALATE = "ESCALATE"
# Sentinel returned for forbidden actions (delete_*). Caller MUST
# check for `None` before applying anything.
FORBIDDEN = None


# Keys are action_type strings used across the agent modules. The
# value is the risk level — None means forbidden.
DECISION_MATRIX: dict[str, Optional[str]] = {
    # ---- reversible, safe ----
    "set_label": AUTO,
    "set_priority": AUTO,
    "add_comment": AUTO,
    "suggest_assignee": NOTIFY,
    "record_risk": AUTO,
    "record_velocity": AUTO,
    "weekly_status_report": AUTO,
    "insight_report": AUTO,
    # ---- changes other people's work — needs confirmation ----
    "reassign_task": CONFIRM,
    "change_deadline": CONFIRM,
    "create_issue_batch": CONFIRM,  # PLANNER's batch (60 tasks)
    "create_single_issue": AUTO,    # one issue from explicit goal is fine
    # ---- strategic / expensive — humans only ----
    "simplify_scope": ESCALATE,
    "hire_freelancer": ESCALATE,
    "escalate_to_pm": ESCALATE,
    # ---- forbidden ----
    "delete_issue": FORBIDDEN,
    "delete_project": FORBIDDEN,
    "delete_anything": FORBIDDEN,
}


def decide(action_type: str, *, on_critical_path: bool = False) -> Optional[str]:
    """Return the risk level for an action, or ``None`` if forbidden.

    If ``on_critical_path`` is True, escalate one notch (AUTO → NOTIFY,
    NOTIFY → CONFIRM, CONFIRM → ESCALATE). Anything already at
    ESCALATE or forbidden stays. The rationale: a label change on a
    blocker is more consequential than a label change on a backlog
    item — push it where a human can see it.
    """
    base = DECISION_MATRIX.get(action_type, CONFIRM)  # unknown -> CONFIRM, never AUTO
    if base is None:
        return None
    if not on_critical_path:
        return base
    return {AUTO: NOTIFY, NOTIFY: CONFIRM, CONFIRM: ESCALATE, ESCALATE: ESCALATE}[base]


def is_forbidden(action_type: str) -> bool:
    return DECISION_MATRIX.get(action_type, CONFIRM) is None


__all__ = [
    "AUTO", "NOTIFY", "CONFIRM", "ESCALATE", "FORBIDDEN",
    "DECISION_MATRIX", "decide", "is_forbidden",
]
