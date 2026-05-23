"""TZ 5.3 — auto-triage scenario invariants.

Three properties make triage safe to run autonomously:

  - **idempotency**: a second trigger on an already-triaged issue
    must NOT re-classify (a human edit re-fires the trigger; we must
    not overwrite what the human just wrote);
  - **suggest, not assign**: ``suggest_assignee`` posts a comment,
    not an ``IssueAssignee`` row;
  - **scope**: agent cannot suggest itself or a non-member.

The full end-to-end Claude loop is exercised in TZ 5.7. Here we
lock the rules in directly so a refactor of the worker can't
regress them silently.
"""

from __future__ import annotations

import pytest

from ai.agent_worker import apply_agent_action, run_agent_body
from ai.models import AIAgentActionLog
from ai.triage import (
    TRIAGE_TOOLS,
    already_triaged,
    build_triage_prompt,
)


@pytest.fixture
def triage_setup(
    db,
    make_user,
    make_workspace,
    make_workspace_member,
    make_project,
    make_ai_config,
    make_issue,
):
    """Workspace + project where the agent and one human candidate
    are active members. Issue is freshly created."""
    from plane.db.models import ProjectMember

    from ai.models import AIAgent

    owner = make_user("owner")
    ws = make_workspace(owner=owner)
    cfg = make_ai_config(ws)
    project = make_project(workspace=ws, created_by=owner)

    agent_user = make_user("agent")
    make_workspace_member(workspace=ws, user=agent_user, role=15)
    ProjectMember.objects.create(
        workspace=ws, project=project, member=agent_user, role=15, is_active=True
    )
    agent = AIAgent.objects.create(user=agent_user, workspace=ws, enabled=True)

    candidate = make_user("candidate")
    make_workspace_member(workspace=ws, user=candidate, role=15)
    ProjectMember.objects.create(
        workspace=ws, project=project, member=candidate, role=15, is_active=True
    )

    issue = make_issue(workspace=ws, project=project, name="please triage")

    class _Setup:
        pass

    s = _Setup()
    s.owner = owner
    s.workspace = ws
    s.project = project
    s.cfg = cfg
    s.agent_user = agent_user
    s.agent = agent
    s.candidate = candidate
    s.issue = issue
    return s


# ---------------------------------------------------------------------------
# Tool-set restriction
# ---------------------------------------------------------------------------


def test_triage_tool_subset_excludes_update_description():
    """Triage MUST NOT offer ``update_description`` to the model —
    triage's job is classification, not rewriting what the human
    just typed."""
    assert "update_description" not in TRIAGE_TOOLS
    assert "set_priority" in TRIAGE_TOOLS
    assert "set_labels" in TRIAGE_TOOLS
    assert "suggest_assignee" in TRIAGE_TOOLS


# ---------------------------------------------------------------------------
# suggest_assignee — comment-mode, not hard-assign
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_suggest_assignee_posts_comment_does_not_assign(triage_setup):
    """The TZ 5.3 invariant. A successful suggestion creates a
    comment by the agent and leaves the assignees list untouched.

    The comment is what makes the suggestion reviewable by the team
    without committing to a wrong assignment."""
    from plane.db.models import IssueComment

    log = apply_agent_action(
        agent=triage_setup.agent,
        issue=triage_setup.issue,
        tool_name="suggest_assignee",
        tool_input={"user_email": triage_setup.candidate.email},
        cfg=triage_setup.cfg,
    )

    assert log.status == AIAgentActionLog.STATUS_APPLIED
    # No hard-assign happened.
    assert list(triage_setup.issue.assignees.all()) == []
    # A comment landed, authored by the agent's user.
    comment = IssueComment.objects.get(id=log.output["comment_id"])
    assert comment.actor_id == triage_setup.agent_user.id
    assert triage_setup.candidate.email in comment.comment_stripped
    assert comment.issue_id == triage_setup.issue.id
    assert comment.project_id == triage_setup.project.id


@pytest.mark.django_db
def test_suggest_assignee_rejects_self(triage_setup):
    """The agent must not suggest its own user account. A "the agent
    should pick itself" instruction would either come from
    confused prompt context or a clumsy injection; either way it's
    a no-op the audit log should record."""
    log = apply_agent_action(
        agent=triage_setup.agent,
        issue=triage_setup.issue,
        tool_name="suggest_assignee",
        tool_input={"user_email": triage_setup.agent_user.email},
        cfg=triage_setup.cfg,
    )
    assert log.status == AIAgentActionLog.STATUS_REJECTED
    assert "itself" in log.error


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_already_triaged_false_then_true(triage_setup):
    """The idempotency predicate flips from False -> True once any
    triage-bucket action has been applied."""
    assert already_triaged(triage_setup.issue.id) is False

    apply_agent_action(
        agent=triage_setup.agent,
        issue=triage_setup.issue,
        tool_name="set_priority",
        tool_input={"priority": "high"},
        cfg=triage_setup.cfg,
    )
    assert already_triaged(triage_setup.issue.id) is True


@pytest.mark.django_db
def test_already_triaged_ignores_rejected_actions(triage_setup):
    """A *rejected* action must NOT count as triage having happened —
    a rejected attempt didn't actually change anything."""
    apply_agent_action(
        agent=triage_setup.agent,
        issue=triage_setup.issue,
        tool_name="set_priority",
        tool_input={"priority": "NOT_A_VALUE"},
        cfg=triage_setup.cfg,
    )
    assert already_triaged(triage_setup.issue.id) is False


@pytest.mark.django_db
def test_run_agent_body_skips_when_all_scenarios_idempotent(
    triage_setup, monkeypatch
):
    """Top-level idempotency: when every scenario's gate is closed,
    the worker returns ``reason='all_scenarios_idempotent'`` so
    observability can tell it was a deliberate skip rather than a
    silent drop.

    We seed an applied row for each scenario bucket (triage:
    ``set_priority``; dedupe: ``add_comment``) so both
    ``already_triaged`` and ``already_deduped`` return True.
    """
    # Triage bucket — set_priority counts.
    AIAgentActionLog.objects.create(
        agent=triage_setup.agent,
        workspace_id=triage_setup.workspace.id,
        project_id=triage_setup.project.id,
        issue_id=triage_setup.issue.id,
        tool_name="set_priority",
        input={"priority": "high"},
        output={"priority": "high", "changed": True},
        status=AIAgentActionLog.STATUS_APPLIED,
    )
    # Dedupe bucket — applied add_comment closes the gate.
    AIAgentActionLog.objects.create(
        agent=triage_setup.agent,
        workspace_id=triage_setup.workspace.id,
        project_id=triage_setup.project.id,
        issue_id=triage_setup.issue.id,
        tool_name="add_comment",
        input={"text": "Possible duplicates: PROJ-1"},
        output={"comment_id": "deadbeef", "comment_chars": 30},
        status=AIAgentActionLog.STATUS_APPLIED,
    )

    # Ensure the worker never reaches Claude or the embed provider —
    # if either path runs we have an idempotency bug.
    from ai import agent_worker

    monkeypatch.setattr(
        agent_worker.providers,
        "ClaudeChat",
        lambda **kw: pytest.fail("worker reached Claude despite idempotency"),
    )

    result = run_agent_body(triage_setup.issue.id)
    assert result["status"] == "skipped"
    assert result["reason"] == "all_scenarios_idempotent"


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_build_triage_prompt_lists_project_labels_and_members(triage_setup):
    """The prompt presents existing labels and active members as a
    closed menu so the model can pick from real options. If the
    project has neither, the prompt explicitly tells the model to
    skip those tools instead of hallucinating."""
    from plane.db.models import Label

    Label.objects.create(
        workspace=triage_setup.workspace,
        project=triage_setup.project,
        name="bug",
        color="#f00",
    )
    Label.objects.create(
        workspace=triage_setup.workspace,
        project=triage_setup.project,
        name="frontend",
        color="#0f0",
    )

    prompt = build_triage_prompt(
        triage_setup.issue,
        context="",
        label_names=["bug", "frontend"],
        member_emails=[triage_setup.candidate.email],
    )
    assert "bug" in prompt
    assert "frontend" in prompt
    assert triage_setup.candidate.email in prompt
    # The agent itself should not be in the suggestion menu — but
    # build_triage_prompt is a pure function; the exclusion is the
    # caller's responsibility (run_agent_body does it).


@pytest.mark.django_db
def test_build_triage_prompt_handles_empty_lists(triage_setup):
    """No labels / no other members: the prompt tells the model to
    skip those tools rather than encouraging it to invent options."""
    prompt = build_triage_prompt(
        triage_setup.issue, context="", label_names=[], member_emails=[]
    )
    assert "нет ни одной метки" in prompt
    assert "нет других участников" in prompt
