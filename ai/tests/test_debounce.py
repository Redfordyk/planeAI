"""Signal debounce: a flurry of saves within 10s collapses to one
enqueued ``reindex_source`` task.
"""

from __future__ import annotations

import pytest
from django.core.cache import cache
from django.db import transaction

from ai import tasks


@pytest.mark.django_db(transaction=True)
def test_five_saves_collapse_to_one_task(
    make_workspace, make_project, make_ai_config, make_issue, monkeypatch
):
    ws = make_workspace()
    prj = make_project(workspace=ws)
    make_ai_config(ws)

    # Capture apply_async calls.
    calls: list[tuple] = []

    def fake_apply_async(args=None, kwargs=None, **kw):
        calls.append(tuple(args or ()))

    monkeypatch.setattr(tasks.reindex_source, "apply_async", fake_apply_async)

    # Wipe debounce keys for the source from any earlier test run.
    if hasattr(cache, "delete_pattern"):
        cache.delete_pattern("ai:reindex_pending:*")

    with transaction.atomic():
        issue = make_issue(workspace=ws, project=prj, name="rapid")
        for i in range(4):
            issue.name = f"rapid-{i}"
            issue.save(update_fields=["name"])

    assert len(calls) == 1, f"expected 1 debounced task, got {len(calls)}"
    # And the captured task points at the right source.
    args = calls[0]
    assert args[2] == "work_item"
    assert args[3] == str(issue.id)
