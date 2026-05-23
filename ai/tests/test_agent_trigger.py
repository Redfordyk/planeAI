"""TZ 5.1 — agent assignment/label trigger + self-loop guard.

Covers the four DoD bullets that have a code surface:
  - assignment to an AIAgent enqueues exactly one task
  - ``ai-agent`` label enqueues exactly one task
  - the loop guard (``agent_acting``) suppresses re-triggering
  - the workspace AI gate suppresses triggers when disabled
"""

from __future__ import annotations

import pytest
from django.core.cache import cache
from django.db import transaction

from ai import tasks
from ai.agent_triggers import agent_acting


def _patch_apply_async(monkeypatch):
    calls: list[tuple] = []

    def fake(args=None, kwargs=None, **kw):
        calls.append(tuple(args or ()))

    monkeypatch.setattr(tasks.run_agent_on_workitem, "apply_async", fake)
    # Also silence the reindex task so unrelated saves don't fail.
    monkeypatch.setattr(tasks.reindex_source, "apply_async", lambda **kw: None)
    return calls


def _clear_debounce():
    if hasattr(cache, "delete_pattern"):
        cache.delete_pattern("ai:agent_pending:*")
        cache.delete_pattern("ai:agent_acting:*")


@pytest.fixture
def make_agent(db, make_user):
    from ai.models import AIAgent

    def _make(workspace, *, enabled=True):
        user = make_user("agent")
        return AIAgent.objects.create(
            user=user, workspace=workspace, enabled=enabled
        )

    return _make


@pytest.mark.django_db(transaction=True)
def test_assignment_to_agent_enqueues_one_task(
    make_workspace, make_project, make_ai_config, make_issue, make_agent, monkeypatch
):
    ws = make_workspace()
    prj = make_project(workspace=ws)
    make_ai_config(ws)
    agent = make_agent(ws)
    calls = _patch_apply_async(monkeypatch)
    _clear_debounce()

    with transaction.atomic():
        issue = make_issue(workspace=ws, project=prj, name="needs triage")
        issue.assignees.add(
            agent.user,
            through_defaults={"workspace_id": ws.id, "project_id": prj.id},
        )
        # Touch the issue so post_save sees the M2M change at commit time.
        issue.save(update_fields=["name"])

    assert len(calls) == 1, f"expected one agent task, got {calls!r}"
    assert calls[0] == (str(issue.id),)


@pytest.mark.django_db(transaction=True)
def test_ai_agent_label_enqueues_one_task(
    make_workspace, make_project, make_ai_config, make_issue, monkeypatch
):
    from plane.db.models import Label

    ws = make_workspace()
    prj = make_project(workspace=ws)
    make_ai_config(ws)
    label = Label.objects.create(
        workspace=ws, project=prj, name="ai-agent", color="#000"
    )
    calls = _patch_apply_async(monkeypatch)
    _clear_debounce()

    with transaction.atomic():
        issue = make_issue(workspace=ws, project=prj, name="auto handle")
        issue.labels.add(
            label,
            through_defaults={"workspace_id": ws.id, "project_id": prj.id},
        )
        issue.save(update_fields=["name"])

    assert len(calls) == 1
    assert calls[0] == (str(issue.id),)


@pytest.mark.django_db(transaction=True)
def test_loop_guard_suppresses_self_trigger(
    make_workspace, make_project, make_ai_config, make_issue, make_agent, monkeypatch
):
    ws = make_workspace()
    prj = make_project(workspace=ws)
    make_ai_config(ws)
    agent = make_agent(ws)
    calls = _patch_apply_async(monkeypatch)
    _clear_debounce()

    issue = make_issue(workspace=ws, project=prj, name="loop test")
    issue.assignees.add(
        agent.user,
        through_defaults={"workspace_id": ws.id, "project_id": prj.id},
    )

    # Simulate the worker acting on the issue: any saves performed
    # inside this block must NOT re-enqueue the agent task.
    with agent_acting(issue.id):
        with transaction.atomic():
            issue.name = "renamed by agent"
            issue.save(update_fields=["name"])

    assert calls == [], (
        f"agent_acting must suppress re-trigger, got {calls!r}"
    )


@pytest.mark.django_db(transaction=True)
def test_workspace_ai_disabled_blocks_trigger(
    make_workspace, make_project, make_issue, make_agent, monkeypatch
):
    # NOTE: no make_ai_config(ws) — workspace gate is closed.
    ws = make_workspace()
    prj = make_project(workspace=ws)
    agent = make_agent(ws)
    calls = _patch_apply_async(monkeypatch)
    _clear_debounce()

    with transaction.atomic():
        issue = make_issue(workspace=ws, project=prj, name="no ai here")
        issue.assignees.add(
            agent.user,
            through_defaults={"workspace_id": ws.id, "project_id": prj.id},
        )
        issue.save(update_fields=["name"])

    assert calls == []


@pytest.mark.django_db(transaction=True)
def test_burst_of_saves_collapses_to_one_task(
    make_workspace, make_project, make_ai_config, make_issue, make_agent, monkeypatch
):
    """Debounce: rapid saves on an assigned issue produce one task."""
    ws = make_workspace()
    prj = make_project(workspace=ws)
    make_ai_config(ws)
    agent = make_agent(ws)
    calls = _patch_apply_async(monkeypatch)
    _clear_debounce()

    with transaction.atomic():
        issue = make_issue(workspace=ws, project=prj, name="burst")
        issue.assignees.add(
            agent.user,
            through_defaults={"workspace_id": ws.id, "project_id": prj.id},
        )
        for i in range(5):
            issue.name = f"burst-{i}"
            issue.save(update_fields=["name"])

    assert len(calls) == 1, f"expected one debounced task, got {len(calls)}"
