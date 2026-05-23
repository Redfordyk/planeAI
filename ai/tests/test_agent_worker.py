"""TZ 5.2 — agent worker safety invariants.

These tests exercise :func:`ai.agent_worker.apply_agent_action`
directly. The end-to-end Claude loop is exercised in TZ 5.7 with a
fully mocked client; here we lock in the three DoD bullets that have
nothing to do with the LLM call itself:

  - запрещённые действия (вне белого списка) невозможны → ``rejected``
  - scope проекта: кросс-проектная Label / неучастник → ``rejected``
  - каждое действие пишется в аудит-лог (applied + rejected both)
"""

from __future__ import annotations

import pytest

from ai.agent_worker import apply_agent_action
from ai.models import AIAgentActionLog


@pytest.fixture
def agent_setup(
    db, make_user, make_workspace, make_workspace_member, make_project, make_ai_config, make_issue
):
    """Build a workspace with one agent user, one human user member,
    a project the agent is a member of, and a starting issue.

    Returned as a small object so each test can pick what it needs.
    """
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

    issue = make_issue(workspace=ws, project=project, name="triage me")

    class _Setup:
        pass

    s = _Setup()
    s.owner = owner
    s.workspace = ws
    s.project = project
    s.cfg = cfg
    s.agent_user = agent_user
    s.agent = agent
    s.issue = issue
    return s


# ---------------------------------------------------------------------------
# White-list enforcement
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_unknown_tool_name_is_rejected_and_logged(agent_setup):
    """A tool name outside AGENT_TOOLS must NEVER reach a handler.

    The Anthropic schema we send already omits these tools so the
    model can't pick them — but the dispatch is defensive.
    """
    log = apply_agent_action(
        agent=agent_setup.agent,
        issue=agent_setup.issue,
        tool_name="delete_issue",  # NOT in AGENT_TOOLS
        tool_input={"issue_id": str(agent_setup.issue.id)},
        cfg=agent_setup.cfg,
    )
    assert log.status == AIAgentActionLog.STATUS_REJECTED
    assert "white-list" in log.error
    # Audit row landed.
    assert AIAgentActionLog.objects.filter(id=log.id).exists()


# ---------------------------------------------------------------------------
# Project scope: cross-project labels rejected
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_set_labels_rejects_cross_project_label(agent_setup, make_project):
    """A Label that exists ONLY in another project must not land on
    our issue. The rejection is per-name; if every name is bad we
    short-circuit with a single rejected row.
    """
    from plane.db.models import Label

    foreign_project = make_project(workspace=agent_setup.workspace)
    foreign_label = Label.objects.create(
        workspace=agent_setup.workspace,
        project=foreign_project,
        name="urgent-from-elsewhere",
        color="#000",
    )

    log = apply_agent_action(
        agent=agent_setup.agent,
        issue=agent_setup.issue,
        tool_name="set_labels",
        tool_input={"labels": [foreign_label.name]},
        cfg=agent_setup.cfg,
    )
    assert log.status == AIAgentActionLog.STATUS_REJECTED
    # Issue acquired no labels.
    assert agent_setup.issue.labels.count() == 0


@pytest.mark.django_db
def test_set_labels_applies_only_same_project_labels(agent_setup, make_project):
    """Mixed batch: one valid, one cross-project. We apply the valid
    name and surface the bad one in ``rejected_cross_project`` so
    the audit reviewer can see what the model tried."""
    from plane.db.models import Label

    foreign_project = make_project(workspace=agent_setup.workspace)
    foreign_label = Label.objects.create(
        workspace=agent_setup.workspace, project=foreign_project, name="bad", color="#f00"
    )
    own_label = Label.objects.create(
        workspace=agent_setup.workspace,
        project=agent_setup.project,
        name="good",
        color="#0f0",
    )

    log = apply_agent_action(
        agent=agent_setup.agent,
        issue=agent_setup.issue,
        tool_name="set_labels",
        tool_input={"labels": [own_label.name, foreign_label.name]},
        cfg=agent_setup.cfg,
    )
    assert log.status == AIAgentActionLog.STATUS_APPLIED
    assert log.output["labels_set"] == 1
    assert log.output["rejected_cross_project"] == ["bad"]
    # Only the in-project label landed.
    assert list(agent_setup.issue.labels.values_list("name", flat=True)) == ["good"]


# ---------------------------------------------------------------------------
# Project scope: cross-project assignee rejected
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_suggest_assignee_rejects_non_member(agent_setup, make_user, make_workspace_member):
    """A user who exists in the workspace but has no ProjectMember
    row for THIS project must not be suggestable. The model could
    be tricked by issue text into naming any address — the scope
    guard catches it BEFORE any Plane write happens (no comment
    leaks for a rejected suggestion)."""
    from plane.db.models import IssueComment

    stranger = make_user("stranger")
    make_workspace_member(workspace=agent_setup.workspace, user=stranger, role=15)
    # Note: NO ProjectMember row for stranger in agent_setup.project.

    log = apply_agent_action(
        agent=agent_setup.agent,
        issue=agent_setup.issue,
        tool_name="suggest_assignee",
        tool_input={"user_email": stranger.email},
        cfg=agent_setup.cfg,
    )
    assert log.status == AIAgentActionLog.STATUS_REJECTED
    assert "not a member" in log.error
    # Comment-mode suggest must not leak a comment for a rejection.
    assert IssueComment.objects.filter(issue_id=agent_setup.issue.id).count() == 0
    # And nothing got hard-assigned either.
    assert stranger not in list(agent_setup.issue.assignees.all())


# ---------------------------------------------------------------------------
# Priority: invalid value rejected, valid value applied + logged
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_set_priority_invalid_value_rejected(agent_setup):
    log = apply_agent_action(
        agent=agent_setup.agent,
        issue=agent_setup.issue,
        tool_name="set_priority",
        tool_input={"priority": "CRITICAL"},  # not in PRIORITY_CHOICES
        cfg=agent_setup.cfg,
    )
    assert log.status == AIAgentActionLog.STATUS_REJECTED
    agent_setup.issue.refresh_from_db()
    # default is "none"
    assert agent_setup.issue.priority == "none"


@pytest.mark.django_db
def test_set_priority_valid_applied_and_audited(agent_setup):
    log = apply_agent_action(
        agent=agent_setup.agent,
        issue=agent_setup.issue,
        tool_name="set_priority",
        tool_input={"priority": "high"},
        cfg=agent_setup.cfg,
    )
    assert log.status == AIAgentActionLog.STATUS_APPLIED
    agent_setup.issue.refresh_from_db()
    assert agent_setup.issue.priority == "high"
    # And an audit row exists with the persisted input.
    persisted = AIAgentActionLog.objects.get(id=log.id)
    assert persisted.tool_name == "set_priority"
    assert persisted.input == {"priority": "high"}
    assert persisted.workspace_id == agent_setup.workspace.id
    assert persisted.project_id == agent_setup.project.id


# ---------------------------------------------------------------------------
# Description: length cap
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_update_description_length_cap_rejected(agent_setup):
    from ai.agent_worker import MAX_DESCRIPTION_CHARS

    too_long = "x" * (MAX_DESCRIPTION_CHARS + 1)
    log = apply_agent_action(
        agent=agent_setup.agent,
        issue=agent_setup.issue,
        tool_name="update_description",
        tool_input={"text": too_long},
        cfg=agent_setup.cfg,
    )
    assert log.status == AIAgentActionLog.STATUS_REJECTED
    agent_setup.issue.refresh_from_db()
    assert agent_setup.issue.description_stripped in ("", None)
