"""reindex_source is idempotent: repeating it on unchanged content
must not pay for any new embeddings.
"""

from __future__ import annotations

import pytest

from ai.models import AIUsageLog, DocumentChunk
from ai.tasks import reindex_source


@pytest.mark.django_db
def test_reindex_skips_when_content_hash_matches(
    make_workspace, make_project, make_ai_config, make_issue, mock_embed
):
    ws = make_workspace()
    prj = make_project(workspace=ws)
    make_ai_config(ws)
    issue = make_issue(
        workspace=ws,
        project=prj,
        name="Add SSO",
        description="Allow workspace admins to enable SAML SSO.",
    )

    # First run: real embedding (mock), chunk rows + usage row created.
    reindex_source.apply(
        args=(str(ws.id), str(prj.id), "work_item", str(issue.id)), throw=True
    )
    n_chunks_1 = DocumentChunk.objects.filter(source_id=issue.id).count()
    n_calls_after_first = len(mock_embed.calls)
    n_usage_after_first = AIUsageLog.objects.filter(
        workspace=ws, feature=AIUsageLog.FEATURE_EMBED
    ).count()
    assert n_chunks_1 >= 1
    assert n_calls_after_first >= 1
    assert n_usage_after_first == 1

    # Second run, no content change: must be a no-op.
    reindex_source.apply(
        args=(str(ws.id), str(prj.id), "work_item", str(issue.id)), throw=True
    )
    assert DocumentChunk.objects.filter(source_id=issue.id).count() == n_chunks_1
    assert len(mock_embed.calls) == n_calls_after_first  # no new embed
    assert (
        AIUsageLog.objects.filter(workspace=ws, feature=AIUsageLog.FEATURE_EMBED).count()
        == n_usage_after_first
    )


@pytest.mark.django_db
def test_reindex_rebuilds_when_content_changes(
    make_workspace, make_project, make_ai_config, make_issue, mock_embed
):
    ws = make_workspace()
    prj = make_project(workspace=ws)
    make_ai_config(ws)
    issue = make_issue(workspace=ws, project=prj, name="Original")

    reindex_source.apply(
        args=(str(ws.id), str(prj.id), "work_item", str(issue.id)), throw=True
    )
    hash_before = DocumentChunk.objects.filter(source_id=issue.id).first().content_hash

    issue.name = "Updated"
    issue.save(update_fields=["name"])
    reindex_source.apply(
        args=(str(ws.id), str(prj.id), "work_item", str(issue.id)), throw=True
    )

    hash_after = DocumentChunk.objects.filter(source_id=issue.id).first().content_hash
    assert hash_before != hash_after
