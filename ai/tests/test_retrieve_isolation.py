"""Critical test: retrieval never crosses workspace boundaries.

This is the single highest-priority safety test in the codebase
(CLAUDE.md invariant 1, ACL.md). Two workspaces, same content,
different users — a query from workspace A must not surface a
chunk that lives in workspace B.
"""

from __future__ import annotations

import pytest

from ai.search import retrieve


@pytest.mark.django_db
def test_retrieve_does_not_leak_across_workspaces(
    make_workspace,
    make_workspace_member,
    make_project,
    make_user,
    make_ai_config,
    make_chunk,
    mock_embed,
):
    from plane.db.models import ProjectMember

    user_a = make_user("a-user")
    user_b = make_user("b-user")

    ws_a = make_workspace("a")
    ws_b = make_workspace("b")
    prj_a = make_project(workspace=ws_a)
    prj_b = make_project(workspace=ws_b)
    make_ai_config(ws_a)
    make_ai_config(ws_b)

    # user_a is a member of ws_a's project only; user_b of ws_b.
    make_workspace_member(workspace=ws_a, user=user_a)
    make_workspace_member(workspace=ws_b, user=user_b)
    ProjectMember.objects.create(
        workspace=ws_a, project=prj_a, member=user_a, role=15, is_active=True
    )
    ProjectMember.objects.create(
        workspace=ws_b, project=prj_b, member=user_b, role=15, is_active=True
    )

    # Identical-content chunks on both sides.
    for i in range(3):
        make_chunk(workspace=ws_a, project=prj_a, content=f"shared {i}")
    for i in range(3):
        make_chunk(workspace=ws_b, project=prj_b, content=f"shared {i}")

    results = retrieve(workspace_id=ws_a.id, user=user_a, query="shared", top_k=10, record=False)
    assert len(results) > 0
    # Every result must belong to ws_a.
    for r in results:
        assert r.workspace_id == str(ws_a.id), (
            f"LEAK: retrieve(ws_a) returned chunk with workspace_id={r.workspace_id}"
        )

    # And the reverse holds for user_b.
    results_b = retrieve(workspace_id=ws_b.id, user=user_b, query="shared", top_k=10, record=False)
    for r in results_b:
        assert r.workspace_id == str(ws_b.id)


@pytest.mark.django_db
def test_retrieve_filters_to_users_allowed_projects(
    make_workspace,
    make_workspace_member,
    make_project,
    make_user,
    make_ai_config,
    make_chunk,
    mock_embed,
):
    from plane.db.models import ProjectMember

    user = make_user("u")
    ws = make_workspace()
    visible = make_project(workspace=ws)
    invisible = make_project(workspace=ws)
    make_ai_config(ws)

    make_workspace_member(workspace=ws, user=user)
    # User is a member of `visible` only.
    ProjectMember.objects.create(
        workspace=ws, project=visible, member=user, role=15, is_active=True
    )

    make_chunk(workspace=ws, project=visible, content="visible content")
    make_chunk(workspace=ws, project=invisible, content="invisible content")

    results = retrieve(workspace_id=ws.id, user=user, query="content", top_k=10, record=False)
    project_ids = {r.project_id for r in results}
    assert str(visible.id) in project_ids
    assert str(invisible.id) not in project_ids


@pytest.mark.django_db
def test_retrieve_with_no_project_membership_still_returns_pages(
    make_workspace,
    make_workspace_member,
    make_user,
    make_ai_config,
    make_chunk,
    mock_embed,
):
    """Workspace member without any ProjectMember row should still
    see page chunks (project_id=NULL), per Plane's page permission
    model where pages are workspace-scoped."""
    user = make_user("u")
    ws = make_workspace()
    make_ai_config(ws)
    make_workspace_member(workspace=ws, user=user)

    page_chunk = make_chunk(
        workspace=ws, project=None, source_type="page", content="page content"
    )

    results = retrieve(workspace_id=ws.id, user=user, query="page", top_k=10, record=False)
    result_ids = {r.id for r in results}
    assert str(page_chunk.id) in result_ids


@pytest.mark.django_db
def test_retrieve_anonymous_returns_empty(
    make_workspace, make_project, make_ai_config, make_chunk, mock_embed
):
    ws = make_workspace()
    prj = make_project(workspace=ws)
    make_ai_config(ws)
    make_chunk(workspace=ws, project=prj, content="anything")

    assert retrieve(workspace_id=ws.id, user=None, query="anything", record=False) == []


@pytest.mark.django_db
def test_retrieve_empty_query_returns_empty(
    make_workspace, make_user, make_ai_config, mock_embed
):
    user = make_user("u")
    ws = make_workspace()
    make_ai_config(ws)
    assert retrieve(workspace_id=ws.id, user=user, query="", record=False) == []
    assert retrieve(workspace_id=ws.id, user=user, query="   ", record=False) == []


@pytest.mark.django_db
def test_retrieve_outsider_returns_empty(
    make_workspace, make_project, make_ai_config, make_user, make_chunk, mock_embed
):
    """User who is not a member of the workspace at all gets nothing,
    even though their auth is valid."""
    ws = make_workspace()
    prj = make_project(workspace=ws)
    make_ai_config(ws)
    make_chunk(workspace=ws, project=prj, content="secret")
    outsider = make_user("outsider")

    assert retrieve(workspace_id=ws.id, user=outsider, query="secret", record=False) == []
