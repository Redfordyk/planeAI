"""AIProjectSettings.exclude_from_ai suppresses ingest enqueue for
that project's issues and comments.
"""

from __future__ import annotations

import pytest
from django.core.cache import cache
from django.db import transaction

from ai import tasks


@pytest.mark.django_db(transaction=True)
def test_excluded_project_issue_does_not_enqueue(
    make_workspace, make_project, make_ai_config, make_issue, monkeypatch
):
    ws = make_workspace()
    excluded = make_project(workspace=ws, exclude_from_ai=True)
    included = make_project(workspace=ws, exclude_from_ai=False)
    make_ai_config(ws)

    calls: list[tuple] = []

    def fake_apply_async(args=None, kwargs=None, **kw):
        calls.append(tuple(args or ()))

    monkeypatch.setattr(tasks.reindex_source, "apply_async", fake_apply_async)

    if hasattr(cache, "delete_pattern"):
        cache.delete_pattern("ai:reindex_pending:*")

    with transaction.atomic():
        excluded_issue = make_issue(workspace=ws, project=excluded, name="hidden")
    assert calls == [], (
        f"excluded project must not enqueue ingest, got {calls!r}"
    )
    assert excluded_issue.project_id == excluded.id

    with transaction.atomic():
        included_issue = make_issue(workspace=ws, project=included, name="public")
    assert len(calls) == 1, "included project should enqueue exactly once"
    assert calls[0][3] == str(included_issue.id)


@pytest.mark.django_db
def test_backfill_skips_excluded_project(
    make_workspace, make_project, make_ai_config, make_issue, monkeypatch
):
    from django.core.management import call_command
    from io import StringIO

    ws = make_workspace()
    excluded = make_project(workspace=ws, exclude_from_ai=True)
    included = make_project(workspace=ws, exclude_from_ai=False)
    make_ai_config(ws)
    make_issue(workspace=ws, project=excluded, name="A1")
    make_issue(workspace=ws, project=excluded, name="A2")
    inc1 = make_issue(workspace=ws, project=included, name="B1")

    calls: list[tuple] = []

    def fake_apply_async(args=None, kwargs=None, **kw):
        calls.append(tuple(args or ()))

    monkeypatch.setattr(tasks.reindex_source, "apply_async", fake_apply_async)

    call_command(
        "backfill_embeddings", workspace=str(ws.id), rate=3, stdout=StringIO()
    )
    # Only the included issue made it to the queue.
    work_item_calls = [c for c in calls if c[2] == "work_item"]
    assert len(work_item_calls) == 1
    assert work_item_calls[0][3] == str(inc1.id)
