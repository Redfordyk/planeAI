"""Smoke-verify ai.usage + ai.guards against live Plane DB.

Install: docker cp scripts/verify_usage_guard.py \
    plane-ce-api-1:/code/plane/db/management/commands/verify_usage_guard.py

Run:     docker compose exec api python manage.py verify_usage_guard

Covers:
  - record_usage writes all four token columns and a positive cost
  - record_usage handles dict-shaped usage (test convenience)
  - tokens_used_this_month sums input+output+cache_creation only
  - budget_status returns (used, budget, exceeded) correctly
  - require_ai_budget returns 403 when disabled, 429 over budget,
    200 when under budget and attaches request.ai_cfg
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace

from django.core.management.base import BaseCommand

from ai.guards import require_ai_budget
from ai.models import AIUsageLog, WorkspaceAIConfig
from ai.usage import budget_status, record_usage, tokens_used_this_month
from plane.db.models import User, Workspace


SLUG = f"usage-smoke-{uuid.uuid4().hex[:8]}"


class _FakeRequest:
    def __init__(self, data=None):
        self.data = data or {}


class _FakeView:
    @require_ai_budget
    def post(self, request, **kwargs):
        # Return 200 + cfg presence so the test can detect normal path.
        from rest_framework.response import Response
        return Response({"ok": True, "has_cfg": hasattr(request, "ai_cfg")}, status=200)


class Command(BaseCommand):
    help = "Smoke-verify ai.usage and ai.guards."

    def handle(self, *args, **opts):
        failures: list[str] = []
        cleanup_workspace_ids: list = []
        cleanup_user_ids: list = []

        def check(label, got, want):
            if got != want:
                failures.append(f"FAIL  {label}: got={got!r} want={want!r}")
            else:
                print(f"ok    {label}")

        try:
            owner = User.objects.create(
                email=f"u+{uuid.uuid4().hex[:6]}@example.test",
                username=f"u-{uuid.uuid4().hex[:6]}",
                first_name="u-owner",
                is_password_autoset=True,
            )
            cleanup_user_ids.append(owner.id)
            ws = Workspace.objects.create(name="U WS", slug=SLUG, owner=owner)
            cleanup_workspace_ids.append(ws.id)
            WorkspaceAIConfig.objects.create(
                workspace=ws,
                enabled=True,
                anthropic_key="sk-ant-fake",
                openai_key="sk-fake",
                monthly_token_budget=10_000,
            )

            # ---- record_usage: anthropic-shaped object --------------
            usage_obj = SimpleNamespace(
                input_tokens=500,
                output_tokens=200,
                cache_read_input_tokens=100,
                cache_creation_input_tokens=50,
            )
            cost = record_usage(
                workspace_id=ws.id,
                user_id=owner.id,
                feature=AIUsageLog.FEATURE_SUMMARIZE,
                model="claude-sonnet-4-6",
                usage=usage_obj,
            )
            check("record_usage: returned positive Decimal cost", cost > Decimal("0"), True)
            row = AIUsageLog.objects.filter(workspace=ws).order_by("-created_at").first()
            check("record_usage: row.input_tokens=500", row.input_tokens, 500)
            check("record_usage: row.output_tokens=200", row.output_tokens, 200)
            check("record_usage: row.cache_read_tokens=100", row.cache_read_tokens, 100)
            check("record_usage: row.cache_creation_tokens=50", row.cache_creation_tokens, 50)
            check("record_usage: cost > 0", row.cost_usd > Decimal("0"), True)
            check("record_usage: feature=summarize", row.feature, AIUsageLog.FEATURE_SUMMARIZE)

            # ---- record_usage: dict-shaped (embedding) ----------------
            n_before = AIUsageLog.objects.filter(workspace=ws).count()
            record_usage(
                workspace_id=ws.id,
                user_id=None,
                feature=AIUsageLog.FEATURE_EMBED,
                model="text-embedding-3-small",
                usage={"total_tokens": 1000},
            )
            check(
                "record_usage(embed dict): 1 new row",
                AIUsageLog.objects.filter(workspace=ws).count(),
                n_before + 1,
            )

            # ---- tokens_used_this_month -----------------------------
            # 500 + 200 + 50 (cache_creation) + 1000 (embed) = 1750
            used = tokens_used_this_month(ws.id)
            check("tokens_used_this_month sums i+o+cache_creation", used, 1750)

            # ---- budget_status under budget ------------------------
            used, budget, exceeded = budget_status(ws.id)
            check("budget_status: used=1750", used, 1750)
            check("budget_status: budget=10000", budget, 10_000)
            check("budget_status: exceeded=False", exceeded, False)

            # ---- guard: 200 path with cfg attached -----------------
            view = _FakeView()
            resp = view.post(_FakeRequest(data={"workspace_id": str(ws.id)}))
            check("guard normal: HTTP 200", resp.status_code, 200)
            check("guard normal: ai_cfg attached", resp.data["has_cfg"], True)

            # ---- guard: 429 when budget exceeded -------------------
            # Bump budget below current usage.
            WorkspaceAIConfig.objects.filter(workspace=ws).update(
                monthly_token_budget=100
            )
            resp = view.post(_FakeRequest(data={"workspace_id": str(ws.id)}))
            check("guard over-budget: HTTP 429", resp.status_code, 429)
            check(
                "guard over-budget: payload mentions budget",
                "budget_tokens" in resp.data,
                True,
            )
            WorkspaceAIConfig.objects.filter(workspace=ws).update(
                monthly_token_budget=10_000
            )

            # ---- guard: 403 when AI disabled -----------------------
            WorkspaceAIConfig.objects.filter(workspace=ws).update(enabled=False)
            resp = view.post(_FakeRequest(data={"workspace_id": str(ws.id)}))
            check("guard disabled: HTTP 403", resp.status_code, 403)

            # ---- guard: 400 when workspace not resolvable ----------
            resp = view.post(_FakeRequest(data={}))
            check("guard no-ws: HTTP 400", resp.status_code, 400)

        finally:
            AIUsageLog.objects.filter(workspace_id__in=cleanup_workspace_ids).delete()
            Workspace.objects.filter(id__in=cleanup_workspace_ids).delete()
            User.objects.filter(id__in=cleanup_user_ids).delete()

        if failures:
            for f in failures:
                self.stdout.write(self.style.ERROR(f))
            raise SystemExit(1)
        self.stdout.write(self.style.SUCCESS("\nALL USAGE/GUARD ASSERTIONS PASSED"))
