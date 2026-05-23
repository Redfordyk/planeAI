"""TZ 6.7 — GDPR release gate.

Three concerns under test:

  1. **Right-to-erasure via the exclude_from_ai flag.** A project
     newly flagged ``exclude_from_ai=True`` triggers the cleanup
     signal added in TZ 6.7 — existing ``DocumentChunk`` rows for
     that project are removed. Without this, the privacy flag would
     only protect *future* writes and the team's GDPR commitment
     would silently leak past data.

  2. **Right-to-erasure via deletion.** Soft-deleting an Issue /
     Comment / Page must delete its DocumentChunk rows (the existing
     ingest signals from TZ 1.4 — pinned here so a future refactor
     can't quietly remove the cleanup branch).

  3. **gdpr_check command** — the 5 preflights that drive the
     GDPR-RELEASE.md gate. Each is verified by toggling exactly one
     input and watching the JSON response.
"""

from __future__ import annotations

import io
import json
import os

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from ai.models import (
    AIProjectSettings,
    AIUsageLog,
    DocumentChunk,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_json(cmd: str, **kw) -> dict:
    out = io.StringIO()
    try:
        call_command(cmd, stdout=out, stderr=io.StringIO(), as_json=True, **kw)
    except CommandError:
        # JSON is printed BEFORE the CommandError is raised — read
        # whatever stdout we got.
        pass
    return json.loads(out.getvalue().strip().splitlines()[-1])


@pytest.fixture
def isolated_env(monkeypatch):
    for k in ("PLANEAI_DPA_CLOSED", "FIELD_ENCRYPTION_KEY"):
        monkeypatch.delenv(k, raising=False)
    # Re-seed FIELD_ENCRYPTION_KEY to a known good value (test conftest
    # sets one too, but we want each test to start from the same baseline).
    monkeypatch.setenv(
        "FIELD_ENCRYPTION_KEY",
        "ZXh0cmFfbG9uZ19rZXlfZm9yX3Rlc3RfdXNlX29ubHk9PT0=",
    )
    return monkeypatch


# ---------------------------------------------------------------------------
# Right-to-erasure: AIProjectSettings.exclude_from_ai signal
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_flipping_exclude_from_ai_purges_existing_chunks(
    make_workspace, make_project, make_chunk
):
    """Project was indexed publicly; admin later marks it private →
    every existing chunk for that project goes away. Without this
    signal, the privacy flag would only block *future* writes — a
    GDPR violation for past data."""
    ws = make_workspace()
    project = make_project(workspace=ws, created_by=ws.owner)
    # Three chunks tied to the project.
    for i in range(3):
        make_chunk(workspace=ws, project=project, chunk_index=i)
    assert DocumentChunk.objects.filter(project=project).count() == 3

    # Flip the flag — signal fires on commit.
    AIProjectSettings.objects.create(project=project, exclude_from_ai=True)

    assert DocumentChunk.objects.filter(project=project).count() == 0


@pytest.mark.django_db(transaction=True)
def test_setting_exclude_false_does_not_reindex(
    make_workspace, make_project, make_chunk
):
    """The reverse direction (declassification): re-saving with
    exclude_from_ai=False is a NO-OP, NOT an auto-reindex. Auto-
    reindex would re-send the content to OpenAI as a side effect of
    a UI toggle — that's a billable decision the operator must
    take explicitly via backfill_embeddings."""
    ws = make_workspace()
    project = make_project(workspace=ws, created_by=ws.owner)
    # Start in the excluded state with no chunks (the normal case
    # for a previously-flagged project).
    settings = AIProjectSettings.objects.create(
        project=project, exclude_from_ai=True
    )
    assert DocumentChunk.objects.filter(project=project).count() == 0

    # Declassify.
    settings.exclude_from_ai = False
    settings.save()
    # Chunks are still absent — no auto-reindex.
    assert DocumentChunk.objects.filter(project=project).count() == 0


# ---------------------------------------------------------------------------
# Right-to-erasure: source deletion
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_soft_delete_issue_purges_chunks(
    make_workspace, make_project, make_issue, make_chunk
):
    """Plane's .delete() does soft-delete (sets deleted_at). The
    post_save signal in ai/signals.py:_on_issue_saved sees
    deleted_at != None and fires delete_chunks. Pin this so a
    future refactor of the soft-delete path doesn't silently lose
    the cleanup."""
    ws = make_workspace()
    project = make_project(workspace=ws, created_by=ws.owner)
    issue = make_issue(workspace=ws, project=project)
    make_chunk(
        workspace=ws, project=project,
        source_type="work_item", source_id=issue.id, chunk_index=0,
    )
    assert DocumentChunk.objects.filter(source_id=issue.id).count() == 1

    from django.utils import timezone

    issue.deleted_at = timezone.now()
    issue.save()

    assert DocumentChunk.objects.filter(source_id=issue.id).count() == 0


# ---------------------------------------------------------------------------
# gdpr_check command
# ---------------------------------------------------------------------------


def test_gdpr_check_dpa_missing(isolated_env, db):
    """No PLANEAI_DPA_CLOSED env → exits non-zero with dpa fail."""
    body = _run_json("gdpr_check", check="dpa")
    assert body["go"] is False
    assert body["results"][0]["status"] == "fail"
    assert "PLANEAI_DPA_CLOSED" in body["results"][0]["detail"]


def test_gdpr_check_dpa_ok(isolated_env, db):
    isolated_env.setenv("PLANEAI_DPA_CLOSED", "2026-07-08")
    body = _run_json("gdpr_check", check="dpa")
    assert body["go"] is True
    assert "2026-07-08" in body["results"][0]["detail"]


def test_gdpr_check_encryption_key_placeholder(isolated_env, db):
    """A CHANGE_ME placeholder is treated as a hard fail — operators
    sometimes set it in dev and forget to rotate on first prod boot."""
    isolated_env.setenv("FIELD_ENCRYPTION_KEY", "CHANGE_ME")
    body = _run_json("gdpr_check", check="encryption_key")
    assert body["go"] is False
    assert "placeholder" in body["results"][0]["detail"]


def test_gdpr_check_encryption_key_too_short(isolated_env, db):
    isolated_env.setenv("FIELD_ENCRYPTION_KEY", "short")
    body = _run_json("gdpr_check", check="encryption_key")
    assert body["go"] is False
    assert "too short" in body["results"][0]["detail"]


def test_gdpr_check_encryption_key_ok(isolated_env, db):
    isolated_env.setenv(
        "FIELD_ENCRYPTION_KEY",
        "ZXh0cmFfbG9uZ19rZXlfZm9yX3Rlc3RfdXNlX29ubHk9PT0=",
    )
    body = _run_json("gdpr_check", check="encryption_key")
    assert body["go"] is True


@pytest.mark.django_db(transaction=True)
def test_gdpr_check_private_clean_ok(
    isolated_env, make_workspace, make_project
):
    """A project flagged exclude_from_ai=True with no chunks passes."""
    isolated_env.setenv("PLANEAI_DPA_CLOSED", "2026-07-08")
    ws = make_workspace()
    project = make_project(workspace=ws, created_by=ws.owner)
    AIProjectSettings.objects.create(project=project, exclude_from_ai=True)
    body = _run_json("gdpr_check", check="private_clean")
    assert body["go"] is True
    private = body["results"][0]
    assert private["status"] == "ok"
    assert private["metrics"]["private_projects"] == 1
    assert private["metrics"]["leaked_chunks"] == 0


@pytest.mark.django_db(transaction=True)
def test_gdpr_check_private_clean_fails_when_chunks_leaked(
    isolated_env, make_workspace, make_project, make_chunk
):
    """If somehow chunks survived in an excluded project (e.g. the
    signal didn't fire because Celery broker was down at the
    moment), the check reports it with project_id so the operator
    can target the cleanup."""
    isolated_env.setenv("PLANEAI_DPA_CLOSED", "2026-07-08")
    ws = make_workspace()
    project = make_project(workspace=ws, created_by=ws.owner)
    # Create chunks BEFORE flagging (no signal yet on this row).
    chunk = make_chunk(workspace=ws, project=project, chunk_index=0)
    # Mark exclude_from_ai but bypass the signal by raw update —
    # mimicking the failure mode the check is designed to catch.
    AIProjectSettings.objects.create(
        project=project, exclude_from_ai=False
    )
    AIProjectSettings.objects.filter(project=project).update(
        exclude_from_ai=True
    )
    # Sanity: chunk still present because we bypassed the signal.
    assert DocumentChunk.objects.filter(project=project).count() == 1

    body = _run_json("gdpr_check", check="private_clean")
    assert body["go"] is False
    private = body["results"][0]
    assert private["status"] == "fail"
    assert private["metrics"]["leaked_chunks"] == 1
    assert str(project.id) in private["metrics"]["offender_project_ids"]


@pytest.mark.django_db(transaction=True)
def test_gdpr_check_deleted_clean_ok(
    isolated_env, make_workspace, make_project, make_issue, make_chunk
):
    """Live issues with chunks — fine. Deleted issues with no chunks
    — fine. The check fails only when a soft-deleted source still
    has chunks pointing at it (signal didn't fire / broker down)."""
    isolated_env.setenv("PLANEAI_DPA_CLOSED", "2026-07-08")
    ws = make_workspace()
    project = make_project(workspace=ws, created_by=ws.owner)
    issue = make_issue(workspace=ws, project=project)
    make_chunk(
        workspace=ws, project=project,
        source_type="work_item", source_id=issue.id, chunk_index=0,
    )
    body = _run_json("gdpr_check", check="deleted_clean")
    assert body["go"] is True


@pytest.mark.django_db(transaction=True)
def test_gdpr_check_deleted_clean_fails_when_stale(
    isolated_env, make_workspace, make_project, make_issue, make_chunk
):
    isolated_env.setenv("PLANEAI_DPA_CLOSED", "2026-07-08")
    ws = make_workspace()
    project = make_project(workspace=ws, created_by=ws.owner)
    issue = make_issue(workspace=ws, project=project)
    make_chunk(
        workspace=ws, project=project,
        source_type="work_item", source_id=issue.id, chunk_index=0,
    )
    # Soft-delete via direct update so the signal does NOT fire
    # (mimicking the broker-down failure mode).
    from django.utils import timezone

    type(issue).objects.filter(pk=issue.pk).update(deleted_at=timezone.now())

    body = _run_json("gdpr_check", check="deleted_clean")
    assert body["go"] is False
    res = body["results"][0]
    assert "work_item" in res["metrics"]["stale_by_type"]


@pytest.mark.django_db(transaction=True)
def test_gdpr_check_feature_complete(
    isolated_env, make_workspace, make_user
):
    isolated_env.setenv("PLANEAI_DPA_CLOSED", "2026-07-08")
    ws = make_workspace()
    u = make_user("u")
    # Valid feature.
    AIUsageLog.objects.create(
        workspace=ws, user=u,
        feature=AIUsageLog.FEATURE_INTENT_SEARCH,
        model="claude-sonnet-4-6",
    )
    body = _run_json("gdpr_check", check="feature_complete")
    assert body["go"] is True

    # Inject an empty-feature row (raw UPDATE bypasses model
    # validation — same as somebody adding a new feature path that
    # forgets to pass feature=).
    AIUsageLog.objects.filter(workspace=ws).update(feature="")
    body = _run_json("gdpr_check", check="feature_complete")
    assert body["go"] is False
    assert body["results"][0]["metrics"]["invalid_rows"] == 1
