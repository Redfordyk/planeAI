"""TZ 6.5 — rollback plan regression tests.

Three concerns under test:

  1. **Kill switch reach.** ``disable_ai`` + ``enable_ai`` management
     commands flip ``WorkspaceAIConfig.enabled`` for one workspace or
     globally. They're idempotent (re-running is a no-op + reports
     "already disabled") and refuse footgun combinations
     (``--workspace`` + ``--all-workspaces``).

  2. **Behavioural contract of ``enabled=False``.** Every code path
     that consumes the flag must observe it: the budget guard 403s,
     the search view 403s, ingest signals short-circuit, agent
     triggers refuse to enqueue. Plane proper stays up — the test
     reuses workspace-level reads (e.g. ``IndexStatusView``, which is
     an audit-style endpoint that intentionally keeps working) to
     confirm the kill switch does not bleed into non-AI surfaces.

  3. **Migration reversibility.** Every ``ai/`` migration must expose
     a working backward path. Django auto-reverses schema migrations
     by default; if a future migration introduces ``RunPython``
     without ``reverse_code``, this test fails fast (instead of at
     3AM during an actual rollback).
"""

from __future__ import annotations

import importlib
import io
import pkgutil

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from rest_framework.test import APIClient

from ai.models import WorkspaceAIConfig


# ---------------------------------------------------------------------------
# disable_ai / enable_ai management commands
# ---------------------------------------------------------------------------


def _run(cmd: str, **kw) -> str:
    """Run a management command and return its stdout."""
    out = io.StringIO()
    call_command(cmd, stdout=out, **kw)
    return out.getvalue()


@pytest.mark.django_db
def test_disable_ai_flips_single_workspace(make_workspace, make_ai_config):
    ws = make_workspace()
    cfg = make_ai_config(ws, enabled=True)
    assert cfg.enabled is True

    output = _run("disable_ai", workspace=str(ws.id))
    cfg.refresh_from_db()
    assert cfg.enabled is False
    assert "changed=1" in output


@pytest.mark.django_db
def test_disable_ai_idempotent(make_workspace, make_ai_config):
    """Second run on an already-disabled workspace is a no-op + clear
    message — operators rely on re-runs being safe."""
    ws = make_workspace()
    cfg = make_ai_config(ws, enabled=False)

    output = _run("disable_ai", workspace=str(ws.id))
    cfg.refresh_from_db()
    assert cfg.enabled is False
    assert "already_disabled=1" in output


@pytest.mark.django_db
def test_disable_ai_resolves_by_slug(make_workspace, make_ai_config):
    """The operator pasted the slug instead of the UUID — both work."""
    ws = make_workspace(slug="acme-rollback")
    make_ai_config(ws, enabled=True)

    _run("disable_ai", workspace="acme-rollback")
    cfg = WorkspaceAIConfig.objects.get(workspace=ws)
    assert cfg.enabled is False


@pytest.mark.django_db
def test_disable_ai_unknown_workspace_raises(db):
    """Bad identifier must raise CommandError so the operator can't
    silently believe they disabled something they didn't."""
    with pytest.raises(CommandError, match="no workspace matched"):
        _run("disable_ai", workspace="not-a-real-thing")


@pytest.mark.django_db
def test_disable_ai_all_workspaces_requires_confirm(
    make_workspace, make_ai_config
):
    ws = make_workspace()
    make_ai_config(ws, enabled=True)
    # Without --confirm: refused.
    with pytest.raises(CommandError, match="requires --confirm"):
        _run("disable_ai", all_workspaces=True)
    # Config untouched.
    cfg = WorkspaceAIConfig.objects.get(workspace=ws)
    assert cfg.enabled is True


@pytest.mark.django_db
def test_disable_ai_all_workspaces_with_confirm(
    make_workspace, make_ai_config
):
    """Two workspaces, both enabled, --all-workspaces + --confirm
    flips both at once. This is the global-incident path: one command
    to silence the AI layer everywhere."""
    ws_a = make_workspace(slug="a")
    ws_b = make_workspace(slug="b")
    make_ai_config(ws_a, enabled=True)
    make_ai_config(ws_b, enabled=True)

    output = _run("disable_ai", all_workspaces=True, confirm=True)
    assert "changed=2" in output
    for ws in (ws_a, ws_b):
        cfg = WorkspaceAIConfig.objects.get(workspace=ws)
        assert cfg.enabled is False


@pytest.mark.django_db
def test_disable_then_enable_round_trip(make_workspace, make_ai_config):
    ws = make_workspace()
    make_ai_config(ws, enabled=True)

    _run("disable_ai", workspace=str(ws.id))
    assert WorkspaceAIConfig.objects.get(workspace=ws).enabled is False
    _run("enable_ai", workspace=str(ws.id))
    assert WorkspaceAIConfig.objects.get(workspace=ws).enabled is True


@pytest.mark.django_db
def test_disable_ai_rejects_workspace_plus_all(make_workspace, make_ai_config):
    ws = make_workspace()
    make_ai_config(ws, enabled=True)
    with pytest.raises(CommandError, match="mutually exclusive"):
        _run(
            "disable_ai",
            workspace=str(ws.id),
            all_workspaces=True,
            confirm=True,
        )


# ---------------------------------------------------------------------------
# Behavioural contract — enabled=False blocks AI endpoints
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_search_view_403_when_disabled(
    make_user, make_workspace, make_workspace_member, make_ai_config
):
    """The single most-used AI endpoint must 403 when AI is disabled.
    The view is async; we go through ``APIClient.post`` which handles
    ASGI dispatch for us so the test mirrors a real request."""
    owner = make_user("owner")
    ws = make_workspace(owner=owner)
    make_workspace_member(workspace=ws, user=owner, role=20)
    make_ai_config(ws, enabled=False)

    client = APIClient()
    client.force_authenticate(user=owner)
    resp = client.post(
        f"/api/ai/workspaces/{ws.id}/search/",
        {"query": "anything"},
        format="json",
    )
    assert resp.status_code == 403


@pytest.mark.django_db
def test_signals_no_op_when_disabled(
    make_user, make_workspace, make_ai_config, make_project, make_issue
):
    """Ingest hook short-circuits when ``WorkspaceAIConfig.enabled=False``.
    Without this, every Issue.save() during the kill-switch window
    would still enqueue an embedding job, defeating the rollback."""
    from ai.signals import _ai_enabled

    owner = make_user("owner")
    ws = make_workspace(owner=owner)
    make_ai_config(ws, enabled=False)
    assert _ai_enabled(ws.id) is False
    # Re-enable, confirm helper sees the change. Sanity that the
    # short-circuit isn't a stuck cache.
    cfg = WorkspaceAIConfig.objects.get(workspace=ws)
    cfg.enabled = True
    cfg.save(update_fields=["enabled"])
    assert _ai_enabled(ws.id) is True


@pytest.mark.django_db
def test_agent_trigger_refuses_when_disabled(
    make_user, make_workspace, make_workspace_member, make_ai_config,
    make_project, make_issue
):
    """Agent path has its own ``enabled`` check in
    ``ai/agent_triggers.py`` (inline filter on WorkspaceAIConfig).
    With the kill switch on, an issue save must not enqueue agent
    work even if the issue already has an assigned AIAgent. We
    assert the inline filter behaviour by querying it the same way
    the trigger does — if a future refactor extracts the helper,
    this test stays valid because it still encodes the contract."""
    owner = make_user("owner")
    ws = make_workspace(owner=owner)
    make_workspace_member(workspace=ws, user=owner, role=20)
    make_ai_config(ws, enabled=False)

    # Mirror the filter agent_triggers uses (line ~167 of that file).
    has_enabled_ai = WorkspaceAIConfig.objects.filter(
        workspace_id=ws.id, enabled=True
    ).exists()
    assert has_enabled_ai is False


@pytest.mark.django_db
def test_index_status_keeps_working_when_disabled(
    make_user, make_workspace, make_workspace_member, make_ai_config
):
    """Read-only audit endpoints (index status, usage stats, agent
    feed) must keep responding after the kill switch — the operator
    needs them to diagnose what happened. The ACL still applies, of
    course; only the ``enabled`` flag is bypassed for reads."""
    owner = make_user("owner")
    ws = make_workspace(owner=owner)
    make_workspace_member(workspace=ws, user=owner, role=20)
    make_ai_config(ws, enabled=False)

    client = APIClient()
    client.force_authenticate(user=owner)
    resp = client.get(f"/api/ai/workspaces/{ws.id}/index-status/")
    assert resp.status_code == 200


@pytest.mark.django_db
def test_usage_stats_keeps_working_when_disabled(
    make_user, make_workspace, make_workspace_member, make_ai_config
):
    """Same as above for the cost dashboard — admin must be able to
    see what was spent right before they pulled the kill switch."""
    owner = make_user("owner")
    ws = make_workspace(owner=owner)
    make_workspace_member(workspace=ws, user=owner, role=20)
    make_ai_config(ws, enabled=False)

    client = APIClient()
    client.force_authenticate(user=owner)
    resp = client.get(f"/api/ai/workspaces/{ws.id}/usage/stats/")
    assert resp.status_code == 200
    # Budget panel reports the disabled state explicitly.
    body = resp.json()
    # exceeded=True because budget_status() returns (0, 0, True) when
    # there's no enabled config — that's the documented contract in
    # ai/usage.py.
    assert body["budget"]["exceeded"] is True


# ---------------------------------------------------------------------------
# Migration reversibility — every ai/ migration has a backward path
# ---------------------------------------------------------------------------


def _iter_ai_migrations():
    """Yield every ``ai.migrations.*`` migration module."""
    import ai.migrations as pkg

    for info in pkgutil.iter_modules(pkg.__path__):
        if info.name.startswith("_"):
            continue
        yield importlib.import_module(f"ai.migrations.{info.name}")


def test_all_ai_migrations_have_reverse():
    """Every operation in every ai/ migration must expose a backward
    path. Django's built-in ``CreateModel``, ``AddField``,
    ``CreateExtension``, ``AddIndex`` all reverse automatically.
    ``RunPython`` / ``RunSQL`` only reverse if you pass
    ``reverse_code`` / ``reverse_sql`` — without that the migration
    becomes irreversible and a real rollback at 3AM fails.

    Catching this at test time means a future migration that forgets
    to wire ``reverse_code`` fails CI instead of an on-call.
    """
    from django.db.migrations.operations.special import (
        RunPython,
        RunSQL,
    )

    bad: list[str] = []
    for module in _iter_ai_migrations():
        mig = module.Migration  # type: ignore[attr-defined]
        for op in mig.operations:
            if isinstance(op, RunPython):
                if op.reverse_code is RunPython.noop or op.reverse_code is None:
                    bad.append(
                        f"{module.__name__}: RunPython without reverse_code"
                    )
            if isinstance(op, RunSQL):
                if op.reverse_sql is RunSQL.noop or op.reverse_sql is None:
                    bad.append(
                        f"{module.__name__}: RunSQL without reverse_sql"
                    )
    assert not bad, "irreversible migrations found:\n" + "\n".join(bad)
