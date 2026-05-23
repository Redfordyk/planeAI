"""AI add-on URL config — mounted at /api/ai/ via ai._root_urls.

History note: I (the deploy iteration on 2026-05-23) accidentally
shrunk this file when adding /transcribe/ and /agent/execute/. The
team's TZ 5.6 + 6.x routes were lost. This restores them and ADDS
the new ones, keeping every name the test suite reverses against.
"""

from __future__ import annotations

from django.urls import path

from ai.agent_views import (
    AgentActionListView,
    AgentActionUndoView,
    AgentDetailView,
    AgentListView,
    IssuesTouchedView,
)
from ai.alerting import AlertWebhookView
from ai.health import HealthView
from ai.metrics import MetricsView
from ai.usage_views import UsageStatsView
from ai.views import (
    AgentExecuteView,
    IndexStatusView,
    SearchView,
    TranscribeView,
)


app_name = "ai"

urlpatterns = [
    # --- TZ 1.8 / 2.3 -----------------------------------------------
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
    # --- Voice + interactive agent (2026-05) ------------------------
    path(
        "workspaces/<uuid:workspace_id>/transcribe/",
        TranscribeView.as_view(),
        name="transcribe",
    ),
    path(
        "workspaces/<uuid:workspace_id>/agent/execute/",
        AgentExecuteView.as_view(),
        name="agent-execute",
    ),
    # --- TZ 5.6: agent transparency UI ------------------------------
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
    # --- TZ 6.2: monitoring + alerts --------------------------------
    path("metrics/", MetricsView.as_view(), name="metrics"),
    path("health/", HealthView.as_view(), name="health"),
    path("alerts/webhook/", AlertWebhookView.as_view(), name="alert-webhook"),
    # --- TZ 6.3: usage dashboard ------------------------------------
    path(
        "workspaces/<uuid:workspace_id>/usage/stats/",
        UsageStatsView.as_view(),
        name="usage-stats",
    ),
]
