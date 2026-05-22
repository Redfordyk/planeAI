"""Budget guard returns 429 when month-to-date tokens >= budget."""

from __future__ import annotations

from decimal import Decimal

import pytest

from ai.guards import require_ai_budget
from ai.models import AIUsageLog


class _Req:
    def __init__(self, data=None):
        self.data = data or {}


class _View:
    @require_ai_budget
    def post(self, request, **kwargs):
        from rest_framework.response import Response

        return Response({"ok": True, "has_cfg": hasattr(request, "ai_cfg")}, status=200)


@pytest.mark.django_db
def test_under_budget_returns_200(make_workspace, make_ai_config):
    ws = make_workspace()
    make_ai_config(ws, monthly_token_budget=10_000)

    resp = _View().post(_Req(data={"workspace_id": str(ws.id)}))
    assert resp.status_code == 200
    assert resp.data["has_cfg"] is True


@pytest.mark.django_db
def test_over_budget_returns_429(make_workspace, make_ai_config):
    ws = make_workspace()
    make_ai_config(ws, monthly_token_budget=100)
    AIUsageLog.objects.create(
        workspace=ws,
        feature=AIUsageLog.FEATURE_SUMMARIZE,
        model="claude-sonnet-4-6",
        input_tokens=80,
        output_tokens=30,  # 110 > 100 -> over
        cost_usd=Decimal("0"),
    )

    resp = _View().post(_Req(data={"workspace_id": str(ws.id)}))
    assert resp.status_code == 429
    assert "budget_tokens" in resp.data
    assert resp.data["used_tokens"] == 110


@pytest.mark.django_db
def test_disabled_workspace_returns_403(make_workspace, make_ai_config):
    ws = make_workspace()
    make_ai_config(ws, enabled=False)

    resp = _View().post(_Req(data={"workspace_id": str(ws.id)}))
    assert resp.status_code == 403


@pytest.mark.django_db
def test_no_workspace_returns_400():
    resp = _View().post(_Req(data={}))
    assert resp.status_code == 400
