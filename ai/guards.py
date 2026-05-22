"""View decorators for AI feature endpoints.

``@require_ai_budget`` is the single front door for any view that
spends LLM tokens. It enforces three rules in order:

  1. AI must be enabled at the workspace level
     (``WorkspaceAIConfig.enabled=True``). Otherwise 403.
  2. Monthly token usage (input + output + cache_creation) must be
     under ``WorkspaceAIConfig.monthly_token_budget``. Otherwise 429.
  3. On success, attach the resolved config to ``request.ai_cfg`` so
     view code does not re-query.

DRF view methods only — assumes the first positional after `self` is
a DRF ``request``. URL kwargs carry the workspace id (per Plane
convention, ``slug`` -> Workspace lookup happens at the view layer).
For routes that take ``workspace_id`` directly, the kwarg name is
the lookup key.
"""

from __future__ import annotations

from functools import wraps

from rest_framework import status
from rest_framework.response import Response

from ai.models import WorkspaceAIConfig
from ai.usage import tokens_used_this_month


def _resolve_workspace_id(request, kwargs):
    """Find the workspace id from URL kwargs or request payload.

    Plane routes prefer ``slug`` (a Workspace.slug); when only the
    slug is known we resolve it via a single query. Falls back to a
    direct ``workspace_id`` kwarg / body field.
    """
    if "workspace_id" in kwargs:
        return kwargs["workspace_id"]
    if "slug" in kwargs:
        from django.apps import apps as django_apps

        Workspace = django_apps.get_model("db", "Workspace")
        ws = Workspace.objects.filter(slug=kwargs["slug"]).only("id").first()
        return ws.id if ws else None
    payload = getattr(request, "data", None) or {}
    return payload.get("workspace_id")


def require_ai_budget(view):
    """Decorator: 403 when AI disabled, 429 when monthly budget hit."""

    @wraps(view)
    def wrapper(self, request, *args, **kwargs):
        workspace_id = _resolve_workspace_id(request, kwargs)
        if workspace_id is None:
            return Response(
                {"error": "workspace not resolvable from request"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        cfg = (
            WorkspaceAIConfig.objects.filter(
                workspace_id=workspace_id, enabled=True
            )
            .only("monthly_token_budget", "chat_model", "embed_model")
            .first()
        )
        if cfg is None:
            return Response(
                {"error": "AI disabled for this workspace"},
                status=status.HTTP_403_FORBIDDEN,
            )

        used = tokens_used_this_month(workspace_id)
        if used >= cfg.monthly_token_budget:
            return Response(
                {
                    "error": "Monthly AI budget exceeded",
                    "used_tokens": used,
                    "budget_tokens": cfg.monthly_token_budget,
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        # Attach for the view to read; saves a duplicate query.
        request.ai_cfg = cfg
        return view(self, request, *args, **kwargs)

    return wrapper
