"""Workspace isolation — the highest-priority invariant of the AI
add-on (CLAUDE.md invariant 1).

A retrieval-shaped query for workspace A must never return chunks
that belong to workspace B. This test seeds two workspaces with
identical-content chunks and then verifies that filtering by
workspace_id is honoured.
"""

from __future__ import annotations

import pytest

from ai.models import DocumentChunk


@pytest.mark.django_db
def test_chunks_filter_by_workspace_id(make_workspace, make_project, make_chunk):
    ws_a = make_workspace("a")
    ws_b = make_workspace("b")
    prj_a = make_project(workspace=ws_a)
    prj_b = make_project(workspace=ws_b)

    # Same content on purpose — proves the filter isn't relying on
    # content uniqueness.
    for _ in range(3):
        make_chunk(workspace=ws_a, project=prj_a, content="shared text")
    for _ in range(2):
        make_chunk(workspace=ws_b, project=prj_b, content="shared text")

    qs_a = DocumentChunk.objects.filter(workspace=ws_a)
    qs_b = DocumentChunk.objects.filter(workspace=ws_b)

    assert qs_a.count() == 3
    assert qs_b.count() == 2

    # Cross-check: NO row from ws_a appears in ws_b's filter and vice
    # versa. This is the literal "no leak" assertion that matters in
    # production.
    a_ids = set(qs_a.values_list("id", flat=True))
    b_ids = set(qs_b.values_list("id", flat=True))
    assert a_ids.isdisjoint(b_ids)

    # A retrieval that filters by workspace_id=ws_a.id must not see
    # any of the ws_b chunks even when ordered globally.
    seen_in_a_query = set(
        DocumentChunk.objects.filter(workspace_id=ws_a.id)
        .order_by("created_at" if False else "updated_at")
        .values_list("id", flat=True)
    )
    assert seen_in_a_query == a_ids
    assert seen_in_a_query.isdisjoint(b_ids)


@pytest.mark.django_db
def test_allowed_projects_filter_excludes_other_workspaces(
    make_workspace, make_project, make_workspace_member, make_user
):
    from ai.acl import allowed_projects

    user = make_user("u")
    ws_a = make_workspace("a")
    ws_b = make_workspace("b")
    prj_a = make_project(workspace=ws_a)
    prj_b = make_project(workspace=ws_b)
    make_workspace_member(workspace=ws_a, user=user)
    make_workspace_member(workspace=ws_b, user=user)

    # Add the user as a project member of BOTH projects.
    from plane.db.models import ProjectMember

    ProjectMember.objects.create(
        workspace=ws_a, project=prj_a, member=user, role=15, is_active=True
    )
    ProjectMember.objects.create(
        workspace=ws_b, project=prj_b, member=user, role=15, is_active=True
    )

    a_projects = allowed_projects(user, ws_a.id)
    b_projects = allowed_projects(user, ws_b.id)

    assert prj_a.id in a_projects
    assert prj_b.id not in a_projects
    assert prj_b.id in b_projects
    assert prj_a.id not in b_projects
