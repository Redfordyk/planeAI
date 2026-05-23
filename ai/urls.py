"""AI add-on URL config — mounted at /api/ai/ via ai._root_urls."""

from __future__ import annotations

from django.urls import path

from ai.agent_views import (
    AgentActionListView,
    AgentActionUndoView,
    AgentDetailView,
    AgentListView,
    IssuesTouchedView,
)
from ai.views import IndexStatusView, SearchView


app_name = "ai"

urlpatterns = [
    path(
        "workspaces/<uuid:workspace_id>/index-status/",
        IndexStatusView.as_view(),
        name="index-status",
    ),
    path(
        "workspaces/<uuid:workspace_id>/search/",
        SearchView.as_view(),
        name="search",
    ),
    # --- TZ 5.6: agent transparency UI -----------------------------
    path(
        "workspaces/<uuid:workspace_id>/agent/actions/",
        AgentActionListView.as_view(),
        name="agent-action-list",
    ),
    path(
        "workspaces/<uuid:workspace_id>/agent/actions/<uuid:action_id>/undo/",
        AgentActionUndoView.as_view(),
        name="agent-action-undo",
    ),
    path(
        "workspaces/<uuid:workspace_id>/agents/",
        AgentListView.as_view(),
        name="agent-list",
    ),
    path(
        "workspaces/<uuid:workspace_id>/agents/<uuid:agent_id>/",
        AgentDetailView.as_view(),
        name="agent-detail",
    ),
    path(
        "workspaces/<uuid:workspace_id>/issues/touched/",
        IssuesTouchedView.as_view(),
        name="issues-touched",
    ),
]
