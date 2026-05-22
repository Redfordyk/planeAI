"""AI add-on URL config — mounted at /api/ai/ via ai._root_urls."""

from __future__ import annotations

from django.urls import path

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
]
