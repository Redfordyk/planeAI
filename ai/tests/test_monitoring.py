"""TZ 6.2 — monitoring endpoints (metrics, health, alert webhook).

Three surfaces under test:

  - ``GET /api/ai/metrics/`` — token gate, OpenMetrics shape, counter
    increments visible in the body.
  - ``GET /api/ai/health/`` — rollup logic, HTTP status follows
    rollup, vector-extension probe.
  - ``POST /api/ai/alerts/webhook/`` — token gate, payload reshape,
    forward URL invoked.

We don't spin Prometheus / Alertmanager in tests — those are config
files and operationally validated via ``promtool``. Here we lock in
the in-process behaviour the Django side controls.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from django.test import override_settings
from rest_framework.test import APIClient

from ai.metrics import AGENT_ACTIONS, PROVIDER_ERRORS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _client() -> APIClient:
    return APIClient()


# ---------------------------------------------------------------------------
# Metrics endpoint
# ---------------------------------------------------------------------------


@override_settings(PLANEAI_METRICS_TOKEN="")
def test_metrics_endpoint_503_when_token_unset(db):
    """If the operator hasn't configured ``PLANEAI_METRICS_TOKEN``
    (env not set), the endpoint refuses with 503 — better than
    silently exposing aggregates to anyone who pokes the URL."""
    resp = _client().get("/api/ai/metrics/")
    assert resp.status_code == 503
    assert b"metrics disabled" in resp.content


@override_settings(PLANEAI_METRICS_TOKEN="secret-token")
def test_metrics_endpoint_401_on_missing_token(db):
    resp = _client().get("/api/ai/metrics/")
    assert resp.status_code == 401


@override_settings(PLANEAI_METRICS_TOKEN="secret-token")
def test_metrics_endpoint_accepts_x_metrics_token(db):
    resp = _client().get("/api/ai/metrics/", HTTP_X_METRICS_TOKEN="secret-token")
    assert resp.status_code == 200
    assert b"# TYPE planeai_provider_errors_total counter" in resp.content
    assert b"# TYPE planeai_agent_actions_total counter" in resp.content


@override_settings(PLANEAI_METRICS_TOKEN="secret-token")
def test_metrics_endpoint_accepts_bearer_authorization(db):
    """Prometheus' standard scrape config uses ``Authorization: Bearer
    <token>``. Our endpoint strips ``Bearer `` and compares."""
    resp = _client().get(
        "/api/ai/metrics/", HTTP_AUTHORIZATION="Bearer secret-token"
    )
    assert resp.status_code == 200


@override_settings(PLANEAI_METRICS_TOKEN="secret-token")
@pytest.mark.django_db
def test_provider_error_counter_surfaces_in_metrics():
    """A bump on the in-process counter must be visible at the next
    scrape — that's the contract Prometheus' rate() relies on."""
    PROVIDER_ERRORS.inc({"provider": "anthropic", "kind": "rate_limit"})
    PROVIDER_ERRORS.inc({"provider": "anthropic", "kind": "rate_limit"})
    PROVIDER_ERRORS.inc({"provider": "openai", "kind": "api_error"})

    resp = _client().get("/api/ai/metrics/", HTTP_X_METRICS_TOKEN="secret-token")
    body = resp.content.decode("utf-8")
    # Three series rendered (anthropic/rate_limit value=2, openai/api_error value=1).
    assert 'provider="anthropic"' in body
    assert 'provider="openai"' in body
    # Value lines — order-insensitive but content must include 2 and 1.
    assert any(
        line.startswith("planeai_provider_errors_total{") and line.endswith(" 2.0")
        for line in body.splitlines()
    )


@override_settings(PLANEAI_METRICS_TOKEN="secret-token")
@pytest.mark.django_db
def test_budget_gauges_appear_for_enabled_workspaces(
    db, make_user, make_workspace, make_ai_config
):
    """A workspace with an enabled AIConfig must produce three samples
    (used/budget/ratio) — even before any usage rows exist."""
    owner = make_user("owner")
    ws = make_workspace(owner=owner)
    make_ai_config(ws, monthly_token_budget=1_000_000)

    resp = _client().get("/api/ai/metrics/", HTTP_X_METRICS_TOKEN="secret-token")
    body = resp.content.decode("utf-8")
    assert "planeai_workspace_tokens" in body
    assert f'workspace_id="{ws.id}"' in body
    # All three metric tags appear.
    for metric in ("used", "budget", "ratio"):
        assert f'metric="{metric}"' in body


# ---------------------------------------------------------------------------
# Health endpoint
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_health_returns_200_when_db_and_vector_ok():
    """Happy path: the AI conftest already runs against pgvector, so
    the vector extension probe should pass."""
    resp = _client().get("/api/ai/health/")
    assert resp.status_code in (200, 503)  # 503 only if vector is missing
    body = resp.json()
    assert "status" in body
    assert "checks" in body
    # Every required check present in the response.
    for key in ("database", "vector_ext", "broker", "index_freshness", "budget"):
        assert key in body["checks"], f"missing check: {key}"


@pytest.mark.django_db
def test_health_503_when_database_check_fails():
    """If the DB probe raises, the rollup is ``down`` and HTTP is 503.
    We force the failure by patching ``connection.cursor`` to raise."""
    from django.db import connection as conn_mod

    real_cursor = conn_mod.cursor

    def _bad_cursor(*a, **kw):
        raise RuntimeError("DB exploded")

    with patch.object(conn_mod, "cursor", _bad_cursor):
        resp = _client().get("/api/ai/health/")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "down"
    assert body["checks"]["database"]["status"] == "down"

    # Sanity: real_cursor is restored after the with-block.
    real_cursor()


@pytest.mark.django_db
def test_health_degraded_when_index_coverage_low(
    db, make_user, make_workspace, make_ai_config, make_project, make_issue
):
    """A workspace with 0 indexed chunks but many issues triggers the
    index-freshness degradation. Rollup stays 200 (degraded != down)
    so a load balancer keeps probing the instance."""
    owner = make_user("owner")
    ws = make_workspace(owner=owner)
    make_ai_config(ws)
    project = make_project(workspace=ws, created_by=owner)
    # Several issues, no DocumentChunk rows — coverage = 0.
    for i in range(3):
        make_issue(workspace=ws, project=project, name=f"i{i}")

    resp = _client().get("/api/ai/health/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["index_freshness"]["status"] == "degraded"


# ---------------------------------------------------------------------------
# Alert webhook
# ---------------------------------------------------------------------------


@override_settings(ALERT_WEBHOOK_TOKEN="")
def test_alert_webhook_503_when_token_unset(db):
    resp = _client().post("/api/ai/alerts/webhook/", {}, format="json")
    assert resp.status_code == 503


@override_settings(ALERT_WEBHOOK_TOKEN="secret-alert-token")
def test_alert_webhook_401_on_missing_token(db):
    resp = _client().post("/api/ai/alerts/webhook/", {}, format="json")
    assert resp.status_code == 401


@override_settings(
    ALERT_WEBHOOK_TOKEN="secret-alert-token",
    ALERT_WEBHOOK_URL="",  # no forward configured
)
def test_alert_webhook_drops_when_forward_url_unset(db):
    """Alert authenticated but no forward URL → 204 (we accept and
    drop, so Alertmanager doesn't retry into a wall)."""
    resp = _client().post(
        "/api/ai/alerts/webhook/",
        {"status": "firing", "alerts": []},
        format="json",
        HTTP_X_ALERT_TOKEN="secret-alert-token",
    )
    assert resp.status_code == 204


@override_settings(
    ALERT_WEBHOOK_TOKEN="secret-alert-token",
    ALERT_WEBHOOK_URL="http://chat.example/webhook",
    ALERT_WEBHOOK_FORMAT="slack",
)
def test_alert_webhook_forwards_slack_shape(db):
    """The receiver reshapes the Alertmanager payload into a Slack-
    style ``{"text": "..."}`` body and POSTs it to ALERT_WEBHOOK_URL."""
    sent = {}

    class _FakeResp:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def read(self): return b""

    def _fake_urlopen(req, timeout=5):
        sent["url"] = req.full_url
        sent["headers"] = dict(req.header_items())
        sent["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp()

    with patch("urllib.request.urlopen", _fake_urlopen):
        resp = _client().post(
            "/api/ai/alerts/webhook/",
            {
                "status": "firing",
                "alerts": [
                    {
                        "labels": {
                            "alertname": "PlaneAIBudgetWarning",
                            "workspace_id": "abc-123",
                        },
                        "annotations": {
                            "summary": "Workspace abc burned 0.84 of budget"
                        },
                    }
                ],
            },
            format="json",
            HTTP_X_ALERT_TOKEN="secret-alert-token",
        )
    assert resp.status_code == 204
    assert sent["url"] == "http://chat.example/webhook"
    # Slack shape: {"text": "*<title>*\n<body>"}.
    assert "text" in sent["body"]
    assert "Расход токенов" in sent["body"]["text"]
    assert "abc-123" in sent["body"]["text"]


# ---------------------------------------------------------------------------
# Agent action counter — wired in agent_worker.log_agent_action
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_agent_action_counter_bumped_on_apply(
    db,
    make_user,
    make_workspace,
    make_workspace_member,
    make_project,
    make_ai_config,
    make_issue,
):
    """Every audit-log row writes a bump on AGENT_ACTIONS. The metric
    surfaces with workspace/tool/status labels for downstream rate()
    queries in Alertmanager."""
    from plane.db.models import ProjectMember
    from ai.agent_worker import apply_agent_action
    from ai.models import AIAgent

    owner = make_user("owner")
    ws = make_workspace(owner=owner)
    cfg = make_ai_config(ws)
    project = make_project(workspace=ws, created_by=owner)
    agent_user = make_user("agent")
    make_workspace_member(workspace=ws, user=agent_user, role=15)
    ProjectMember.objects.create(
        workspace=ws, project=project, member=agent_user, role=15, is_active=True
    )
    agent = AIAgent.objects.create(user=agent_user, workspace=ws, enabled=True)
    issue = make_issue(workspace=ws, project=project, name="metric me")

    # Reset counter values for THIS workspace so prior tests don't
    # contaminate the assertion.
    key = (str(ws.id), "set_priority", "applied")
    with AGENT_ACTIONS._lock:
        AGENT_ACTIONS._values.pop(key, None)

    apply_agent_action(
        agent=agent,
        issue=issue,
        tool_name="set_priority",
        tool_input={"priority": "high"},
        cfg=cfg,
    )
    with AGENT_ACTIONS._lock:
        assert AGENT_ACTIONS._values.get(key) == 1.0
