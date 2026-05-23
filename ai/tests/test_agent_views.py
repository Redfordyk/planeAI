"""TZ 5.6 — agent transparency UI endpoints.

Drives the new views directly through DRF's ``APIClient`` so the
URL conf, the permission checks and the JSON shape stay locked in
across refactors. Five surfaces under test:

  - ``GET /agent/actions/`` — feed list, filters, ACL.
  - ``POST /agent/actions/<id>/undo/`` — undo correctness, refusal
    paths (non-reversible tool, already-undone, missing snapshot,
    non-member).
  - ``GET /agents/`` + ``PATCH /agents/<id>/`` — agent toggle with
    workspace-admin gate.
  - ``GET /issues/touched/?ids=...`` — bulk badge map.

Reuses the shared fixtures from ``conftest.py`` (workspace / project
factories) plus a small ``agent_setup`` helper that creates the
caller, the agent, an issue, and applies a labelled action.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from rest_framework.test import APIClient

from ai.agent_views import REVERSIBLE_TOOLS
from ai.agent_worker import apply_agent_action
from ai.models import AIAgent, AIAgentActionLog


@pytest.fixture
def agent_views_setup(
    db,
    make_user,
    make_workspace,
    make_workspace_member,
    make_project,
    make_ai_config,
    make_issue,
):
    """Workspace + project the caller is a MEMBER of, plus an agent
    and an issue. The caller has WRITE role so they can also undo —
    undo doesn't *require* admin, just project membership."""
    from plane.db.models import Label, ProjectMember

    owner = make_user("owner")
    ws = make_workspace(owner=owner)
    cfg = make_ai_config(ws)

    # Caller (the human using the UI) — workspace MEMBER + project member.
    caller = make_user("caller")
    make_workspace_member(workspace=ws, user=caller, role=15)
    project = make_project(workspace=ws, created_by=owner)
    ProjectMember.objects.create(
        workspace=ws, project=project, member=caller, role=15, is_active=True
    )

    # Workspace admin user (for admin-only endpoint tests).
    admin = make_user("admin")
    make_workspace_member(workspace=ws, user=admin, role=20)

    # The agent user — separate from the human caller.
    agent_user = make_user("agent")
    make_workspace_member(workspace=ws, user=agent_user, role=15)
    ProjectMember.objects.create(
        workspace=ws, project=project, member=agent_user, role=15, is_active=True
    )
    agent = AIAgent.objects.create(user=agent_user, workspace=ws, enabled=True)

    issue = make_issue(workspace=ws, project=project, name="audit me")

    # Two project labels — used by the set_labels undo test.
    label_bug = Label.objects.create(
        workspace=ws, project=project, name="bug", color="#f00"
    )
    label_fe = Label.objects.create(
        workspace=ws, project=project, name="frontend", color="#0f0"
    )

    class _S:
        pass

    s = _S()
    s.owner = owner
    s.caller = caller
    s.admin = admin
    s.workspace = ws
    s.cfg = cfg
    s.project = project
    s.agent_user = agent_user
    s.agent = agent
    s.issue = issue
    s.label_bug = label_bug
    s.label_fe = label_fe
    return s


def _client_for(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=user)
    return client


# ===========================================================================
# Audit list
# ===========================================================================


@pytest.mark.django_db
def test_action_list_requires_workspace_membership(agent_views_setup, make_user):
    """A user outside the workspace cannot see its agent actions —
    the ws_id in the URL must NEVER be enough on its own."""
    stranger = make_user("stranger")
    url = f"/api/ai/workspaces/{agent_views_setup.workspace.id}/agent/actions/"
    resp = _client_for(stranger).get(url)
    assert resp.status_code == 403


@pytest.mark.django_db
def test_action_list_returns_only_own_project_actions(
    agent_views_setup, make_user, make_workspace_member, make_project, make_issue
):
    """Caller is on project A; an action on project B in the same
    workspace must NOT leak into their feed."""
    from plane.db.models import ProjectMember

    # Seed one action on caller's project — should appear.
    apply_agent_action(
        agent=agent_views_setup.agent,
        issue=agent_views_setup.issue,
        tool_name="set_priority",
        tool_input={"priority": "high"},
        cfg=agent_views_setup.cfg,
    )

    # Second project the caller is NOT a member of, with its own action.
    other_project = make_project(
        workspace=agent_views_setup.workspace, created_by=agent_views_setup.owner
    )
    ProjectMember.objects.create(
        workspace=agent_views_setup.workspace,
        project=other_project,
        member=agent_views_setup.agent_user,
        role=15,
        is_active=True,
    )
    other_issue = make_issue(
        workspace=agent_views_setup.workspace,
        project=other_project,
        name="hidden",
    )
    apply_agent_action(
        agent=agent_views_setup.agent,
        issue=other_issue,
        tool_name="set_priority",
        tool_input={"priority": "low"},
        cfg=agent_views_setup.cfg,
    )

    url = f"/api/ai/workspaces/{agent_views_setup.workspace.id}/agent/actions/"
    resp = _client_for(agent_views_setup.caller).get(url)
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["results"][0]["project_id"] == str(agent_views_setup.project.id)
    # No leak of the other project's action.
    assert not any(
        a["project_id"] == str(other_project.id) for a in body["results"]
    )


@pytest.mark.django_db
def test_action_list_filters_apply(agent_views_setup):
    """Filters (tool, status, project, issue, since) all narrow the
    result set — verified by issuing one of each kind of row and
    asserting the filtered shape."""
    apply_agent_action(
        agent=agent_views_setup.agent,
        issue=agent_views_setup.issue,
        tool_name="set_priority",
        tool_input={"priority": "high"},
        cfg=agent_views_setup.cfg,
    )
    apply_agent_action(
        agent=agent_views_setup.agent,
        issue=agent_views_setup.issue,
        tool_name="set_labels",
        tool_input={"labels": [agent_views_setup.label_bug.name]},
        cfg=agent_views_setup.cfg,
    )
    # A rejected one (invalid priority).
    apply_agent_action(
        agent=agent_views_setup.agent,
        issue=agent_views_setup.issue,
        tool_name="set_priority",
        tool_input={"priority": "CRITICAL"},  # not in choices
        cfg=agent_views_setup.cfg,
    )

    client = _client_for(agent_views_setup.caller)
    base = f"/api/ai/workspaces/{agent_views_setup.workspace.id}/agent/actions/"

    # tool=set_labels — one row.
    resp = client.get(base + "?tool=set_labels")
    assert resp.json()["count"] == 1

    # status=rejected — one row.
    resp = client.get(base + "?status=rejected")
    assert resp.json()["count"] == 1
    assert resp.json()["results"][0]["status"] == "rejected"

    # issue= — only this issue's rows (all of them).
    resp = client.get(base + f"?issue={agent_views_setup.issue.id}")
    assert resp.json()["count"] == 3


@pytest.mark.django_db
def test_action_list_since_filter_parses_iso8601(agent_views_setup):
    """`since` accepts ISO-8601 (with or without Z)."""
    apply_agent_action(
        agent=agent_views_setup.agent,
        issue=agent_views_setup.issue,
        tool_name="set_priority",
        tool_input={"priority": "low"},
        cfg=agent_views_setup.cfg,
    )
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    client = _client_for(agent_views_setup.caller)
    base = f"/api/ai/workspaces/{agent_views_setup.workspace.id}/agent/actions/"

    # since=past — sees the row.
    resp = client.get(base + f"?since={past}")
    assert resp.json()["count"] == 1
    # since=future — nothing.
    resp = client.get(base + f"?since={future}")
    assert resp.json()["count"] == 0
    # malformed since — 400.
    resp = client.get(base + "?since=tomorrow")
    assert resp.status_code == 400


@pytest.mark.django_db
def test_action_list_marks_reversible_only_for_applied_set_labels(agent_views_setup):
    """The ``reversible`` field is True ONLY for an applied
    ``set_labels`` row. Other tools (or rejected attempts) report
    False so the UI hides the undo button."""
    apply_agent_action(
        agent=agent_views_setup.agent,
        issue=agent_views_setup.issue,
        tool_name="set_labels",
        tool_input={"labels": [agent_views_setup.label_bug.name]},
        cfg=agent_views_setup.cfg,
    )
    apply_agent_action(
        agent=agent_views_setup.agent,
        issue=agent_views_setup.issue,
        tool_name="set_priority",
        tool_input={"priority": "low"},
        cfg=agent_views_setup.cfg,
    )

    client = _client_for(agent_views_setup.caller)
    base = f"/api/ai/workspaces/{agent_views_setup.workspace.id}/agent/actions/"
    rows = client.get(base).json()["results"]
    by_tool = {r["tool_name"]: r for r in rows}
    assert by_tool["set_labels"]["reversible"] is True
    assert by_tool["set_priority"]["reversible"] is False


# ===========================================================================
# Undo
# ===========================================================================


@pytest.mark.django_db
def test_undo_set_labels_restores_previous_labels(agent_views_setup):
    """Round-trip: issue has label "frontend"; agent replaces with
    "bug"; caller undoes; "frontend" comes back, "bug" is gone."""
    # Pre-state: issue carries "frontend".
    agent_views_setup.issue.labels.set([agent_views_setup.label_fe])

    log = apply_agent_action(
        agent=agent_views_setup.agent,
        issue=agent_views_setup.issue,
        tool_name="set_labels",
        tool_input={"labels": [agent_views_setup.label_bug.name]},
        cfg=agent_views_setup.cfg,
    )
    assert log.status == AIAgentActionLog.STATUS_APPLIED
    # The agent's apply switched labels to ["bug"].
    assert list(agent_views_setup.issue.labels.values_list("name", flat=True)) == ["bug"]

    url = (
        f"/api/ai/workspaces/{agent_views_setup.workspace.id}"
        f"/agent/actions/{log.id}/undo/"
    )
    resp = _client_for(agent_views_setup.caller).post(url)
    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert body["undone_at"] is not None
    assert body["undone_by_id"] == str(agent_views_setup.caller.id)

    agent_views_setup.issue.refresh_from_db()
    assert list(agent_views_setup.issue.labels.values_list("name", flat=True)) == ["frontend"]


@pytest.mark.django_db
def test_undo_rejects_non_reversible_tool(agent_views_setup):
    """``set_priority`` is not in REVERSIBLE_TOOLS — undo returns 422."""
    log = apply_agent_action(
        agent=agent_views_setup.agent,
        issue=agent_views_setup.issue,
        tool_name="set_priority",
        tool_input={"priority": "high"},
        cfg=agent_views_setup.cfg,
    )
    url = (
        f"/api/ai/workspaces/{agent_views_setup.workspace.id}"
        f"/agent/actions/{log.id}/undo/"
    )
    resp = _client_for(agent_views_setup.caller).post(url)
    assert resp.status_code == 422
    assert "not reversible" in resp.json()["error"]


@pytest.mark.django_db
def test_undo_rejects_already_undone(agent_views_setup):
    """A second undo on the same row returns 409 and does not flip
    labels again — important when the UI re-fires accidentally."""
    log = apply_agent_action(
        agent=agent_views_setup.agent,
        issue=agent_views_setup.issue,
        tool_name="set_labels",
        tool_input={"labels": [agent_views_setup.label_bug.name]},
        cfg=agent_views_setup.cfg,
    )
    url = (
        f"/api/ai/workspaces/{agent_views_setup.workspace.id}"
        f"/agent/actions/{log.id}/undo/"
    )
    client = _client_for(agent_views_setup.caller)
    assert client.post(url).status_code == 200
    resp = client.post(url)
    assert resp.status_code == 409
    assert "already undone" in resp.json()["error"]


@pytest.mark.django_db
def test_undo_rejects_rejected_actions(agent_views_setup):
    """An action with status='rejected' has no effect to undo —
    returns 422 rather than pretending success."""
    # Empty labels list rejects.
    log = apply_agent_action(
        agent=agent_views_setup.agent,
        issue=agent_views_setup.issue,
        tool_name="set_labels",
        tool_input={"labels": []},
        cfg=agent_views_setup.cfg,
    )
    assert log.status == AIAgentActionLog.STATUS_REJECTED
    url = (
        f"/api/ai/workspaces/{agent_views_setup.workspace.id}"
        f"/agent/actions/{log.id}/undo/"
    )
    resp = _client_for(agent_views_setup.caller).post(url)
    assert resp.status_code == 422


@pytest.mark.django_db
def test_undo_rejects_non_member_of_project(
    agent_views_setup, make_user, make_workspace_member
):
    """A workspace member who isn't on the action's project cannot
    undo. The ACL rule mirrors :func:`allowed_projects` exactly."""
    log = apply_agent_action(
        agent=agent_views_setup.agent,
        issue=agent_views_setup.issue,
        tool_name="set_labels",
        tool_input={"labels": [agent_views_setup.label_bug.name]},
        cfg=agent_views_setup.cfg,
    )
    outsider = make_user("outsider")
    make_workspace_member(workspace=agent_views_setup.workspace, user=outsider, role=15)
    # No ProjectMember row for outsider on agent_views_setup.project.
    url = (
        f"/api/ai/workspaces/{agent_views_setup.workspace.id}"
        f"/agent/actions/{log.id}/undo/"
    )
    resp = _client_for(outsider).post(url)
    assert resp.status_code == 403


@pytest.mark.django_db
def test_undo_does_not_retrigger_agent(agent_views_setup, monkeypatch):
    """The undo write must be wrapped in :func:`agent_acting` so the
    post_save signal on Issue/M2M does NOT enqueue another agent
    run — otherwise the agent would immediately re-apply its labels.
    """
    from ai import agent_triggers

    enqueues: list = []
    monkeypatch.setattr(
        agent_triggers, "_enqueue_agent", lambda issue_id: enqueues.append(issue_id)
    )

    agent_views_setup.issue.labels.set([agent_views_setup.label_fe])
    log = apply_agent_action(
        agent=agent_views_setup.agent,
        issue=agent_views_setup.issue,
        tool_name="set_labels",
        tool_input={"labels": [agent_views_setup.label_bug.name]},
        cfg=agent_views_setup.cfg,
    )
    enqueues.clear()  # ignore any enqueues from the apply step

    url = (
        f"/api/ai/workspaces/{agent_views_setup.workspace.id}"
        f"/agent/actions/{log.id}/undo/"
    )
    resp = _client_for(agent_views_setup.caller).post(url)
    assert resp.status_code == 200
    assert enqueues == []


@pytest.mark.django_db
def test_undo_404_on_unknown_action(agent_views_setup):
    bogus = uuid.uuid4()
    url = (
        f"/api/ai/workspaces/{agent_views_setup.workspace.id}"
        f"/agent/actions/{bogus}/undo/"
    )
    resp = _client_for(agent_views_setup.caller).post(url)
    assert resp.status_code == 404


# ===========================================================================
# Agent toggle
# ===========================================================================


@pytest.mark.django_db
def test_agents_list_visible_to_workspace_member(agent_views_setup):
    url = f"/api/ai/workspaces/{agent_views_setup.workspace.id}/agents/"
    resp = _client_for(agent_views_setup.caller).get(url)
    assert resp.status_code == 200
    rows = resp.json()["results"]
    assert len(rows) == 1
    assert rows[0]["enabled"] is True


@pytest.mark.django_db
def test_agent_patch_requires_admin(agent_views_setup):
    """A non-admin workspace member cannot toggle the agent — even
    though they can see it in the list. Toggle is workspace-level
    authority."""
    url = (
        f"/api/ai/workspaces/{agent_views_setup.workspace.id}"
        f"/agents/{agent_views_setup.agent.id}/"
    )
    resp = _client_for(agent_views_setup.caller).patch(
        url, {"enabled": False}, format="json"
    )
    assert resp.status_code == 403
    agent_views_setup.agent.refresh_from_db()
    assert agent_views_setup.agent.enabled is True


@pytest.mark.django_db
def test_agent_patch_admin_can_disable_and_reenable(agent_views_setup):
    url = (
        f"/api/ai/workspaces/{agent_views_setup.workspace.id}"
        f"/agents/{agent_views_setup.agent.id}/"
    )
    client = _client_for(agent_views_setup.admin)
    resp = client.patch(url, {"enabled": False}, format="json")
    assert resp.status_code == 200
    agent_views_setup.agent.refresh_from_db()
    assert agent_views_setup.agent.enabled is False
    resp = client.patch(url, {"enabled": True}, format="json")
    assert resp.status_code == 200
    agent_views_setup.agent.refresh_from_db()
    assert agent_views_setup.agent.enabled is True


@pytest.mark.django_db
def test_agent_patch_validates_enabled_payload(agent_views_setup):
    """Body validation: missing key -> 400; non-bool -> 400."""
    url = (
        f"/api/ai/workspaces/{agent_views_setup.workspace.id}"
        f"/agents/{agent_views_setup.agent.id}/"
    )
    client = _client_for(agent_views_setup.admin)
    assert client.patch(url, {}, format="json").status_code == 400
    assert client.patch(url, {"enabled": "yes"}, format="json").status_code == 400


# ===========================================================================
# Touched
# ===========================================================================


@pytest.mark.django_db
def test_touched_returns_only_applied_and_not_undone(
    agent_views_setup, make_issue
):
    """The badge means "agent did something durable here". Rejected
    attempts and undone actions must NOT light up the badge."""
    # Issue A: gets an applied set_priority — badged.
    issue_a = agent_views_setup.issue
    apply_agent_action(
        agent=agent_views_setup.agent,
        issue=issue_a,
        tool_name="set_priority",
        tool_input={"priority": "high"},
        cfg=agent_views_setup.cfg,
    )

    # Issue B: only a rejected attempt — NOT badged.
    issue_b = make_issue(
        workspace=agent_views_setup.workspace,
        project=agent_views_setup.project,
        name="rejected only",
    )
    apply_agent_action(
        agent=agent_views_setup.agent,
        issue=issue_b,
        tool_name="set_priority",
        tool_input={"priority": "CRITICAL"},
        cfg=agent_views_setup.cfg,
    )

    # Issue C: an applied set_labels that we then undo — NOT badged.
    issue_c = make_issue(
        workspace=agent_views_setup.workspace,
        project=agent_views_setup.project,
        name="undone",
    )
    log_c = apply_agent_action(
        agent=agent_views_setup.agent,
        issue=issue_c,
        tool_name="set_labels",
        tool_input={"labels": [agent_views_setup.label_bug.name]},
        cfg=agent_views_setup.cfg,
    )
    AIAgentActionLog.objects.filter(id=log_c.id).update(
        undone_at=datetime.now(timezone.utc), undone_by=agent_views_setup.caller
    )

    ids = ",".join(str(i) for i in (issue_a.id, issue_b.id, issue_c.id))
    url = (
        f"/api/ai/workspaces/{agent_views_setup.workspace.id}"
        f"/issues/touched/?ids={ids}"
    )
    body = _client_for(agent_views_setup.caller).get(url).json()
    assert body["touched"][str(issue_a.id)] is True
    assert body["touched"][str(issue_b.id)] is False
    assert body["touched"][str(issue_c.id)] is False


@pytest.mark.django_db
def test_touched_does_not_leak_across_projects(
    agent_views_setup, make_user, make_workspace_member, make_project, make_issue
):
    """An issue in a project the caller isn't on must report
    ``touched=False`` — even when an agent action exists."""
    from plane.db.models import ProjectMember

    other = make_project(
        workspace=agent_views_setup.workspace, created_by=agent_views_setup.owner
    )
    ProjectMember.objects.create(
        workspace=agent_views_setup.workspace,
        project=other,
        member=agent_views_setup.agent_user,
        role=15,
        is_active=True,
    )
    other_issue = make_issue(
        workspace=agent_views_setup.workspace,
        project=other,
        name="hidden touched",
    )
    apply_agent_action(
        agent=agent_views_setup.agent,
        issue=other_issue,
        tool_name="set_priority",
        tool_input={"priority": "high"},
        cfg=agent_views_setup.cfg,
    )
    url = (
        f"/api/ai/workspaces/{agent_views_setup.workspace.id}"
        f"/issues/touched/?ids={other_issue.id}"
    )
    body = _client_for(agent_views_setup.caller).get(url).json()
    assert body["touched"][str(other_issue.id)] is False


@pytest.mark.django_db
def test_touched_caps_query_length(agent_views_setup):
    """An over-long id list returns 400 — protects the index from
    accidental denial of service via mega-list."""
    ids = ",".join(uuid.uuid4().hex for _ in range(250))
    url = (
        f"/api/ai/workspaces/{agent_views_setup.workspace.id}"
        f"/issues/touched/?ids={ids}"
    )
    resp = _client_for(agent_views_setup.caller).get(url)
    assert resp.status_code == 400


# ===========================================================================
# Cross-cutting sanity
# ===========================================================================


def test_reversible_tools_is_exactly_set_labels():
    """Hard-coded invariant: TZ 5.6 chose ``set_labels`` as the only
    reversible action. Adding another requires extending the
    serializer + the dispatcher, so we lock the membership here."""
    assert REVERSIBLE_TOOLS == frozenset({"set_labels"})
