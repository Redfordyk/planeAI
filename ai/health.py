"""TZ 6.2 — health endpoint for the AI layer.

Distinct from Plane's general ``/api/healthcheck/``: this one knows
about the AI-specific moving parts — pgvector extension, Celery
broker for indexing/agent queues, the indexing coverage of any
enabled workspaces, and the aggregate budget headroom. It's the
endpoint a deploy probe (or a manual `curl` during incident triage)
hits to answer "is the AI layer actually live, or just the HTTP
listener".

Response shape::

    GET /api/ai/health/
    200 / 503

    {
      "status": "ok" | "degraded" | "down",
      "checks": {
        "database":       {"status": "ok",       "detail": "..."},
        "vector_ext":     {"status": "ok",       "detail": "vector 0.8.2"},
        "broker":         {"status": "ok",       "detail": "redis 22 keys"},
        "index_freshness":{"status": "degraded", "detail": "alpha-ws coverage 0.62"},
        "budget":         {"status": "ok",       "detail": "no workspace over 80%"}
      },
      "version": "<git-sha or pkg version, optional>"
    }

Status rollup:
    - ``ok`` if every check is ``ok``;
    - ``degraded`` if at least one check is degraded but no critical
      check is ``down`` (e.g. one workspace's coverage dropped — the
      AI service is *technically* usable);
    - ``down`` if a critical check (database, vector_ext) is down.

HTTP status reflects the rollup: ``ok`` → 200, ``degraded`` → 200
(load balancers should NOT pull a degraded instance), ``down`` → 503.
"""

from __future__ import annotations

import logging

from django.conf import settings
from django.db import connection
from rest_framework import status as http_status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView


logger = logging.getLogger("plane.ai.health")


# Coverage threshold below which we flag the index as "degraded".
# Same value the search UI uses as its READY gate (TZ 1.8) so the
# alert is consistent with what users see.
INDEX_HEALTH_COVERAGE = 0.90

# Budget ratio threshold for the budget check. The Prometheus alert
# fires at 0.80 (TZ 6.2); the health-endpoint warn comes a bit later
# so that "alert" and "health degraded" don't always co-fire — the
# alert is action-required, degradation is information.
BUDGET_HEALTH_RATIO = 0.85

CHECK_OK = "ok"
CHECK_DEGRADED = "degraded"
CHECK_DOWN = "down"


def _check_database() -> dict:
    """``SELECT 1`` on the default connection. If this fails, we
    cannot answer ANY of the other checks meaningfully — short-circuit
    the rest of the rollup."""
    try:
        with connection.cursor() as c:
            c.execute("SELECT 1")
            c.fetchone()
        return {"status": CHECK_OK, "detail": "select 1 ok"}
    except Exception as exc:  # noqa: BLE001
        return {"status": CHECK_DOWN, "detail": f"{type(exc).__name__}: {exc}"}


def _check_vector_extension() -> dict:
    """The ``vector`` extension must be installed and loaded. Without
    it, every query touching ``ai_document_chunk.embedding`` fails."""
    try:
        with connection.cursor() as c:
            c.execute(
                "SELECT extversion FROM pg_extension WHERE extname='vector'"
            )
            row = c.fetchone()
        if not row:
            return {"status": CHECK_DOWN, "detail": "vector extension not installed"}
        return {"status": CHECK_OK, "detail": f"vector {row[0]}"}
    except Exception as exc:  # noqa: BLE001
        return {"status": CHECK_DOWN, "detail": f"{type(exc).__name__}: {exc}"}


def _check_broker() -> dict:
    """Reach the Celery broker. We don't take the absence of celery
    as a failure for the API process — embedding/agent tasks are
    background concerns. So broker-down rolls up to ``degraded``,
    not ``down``."""
    try:
        broker = getattr(settings, "CELERY_BROKER_URL", "")
        if not broker:
            return {"status": CHECK_DEGRADED, "detail": "CELERY_BROKER_URL unset"}
        if broker.startswith("redis://") or broker.startswith("rediss://"):
            import redis  # type: ignore

            r = redis.Redis.from_url(broker, socket_timeout=2)
            r.ping()
            return {"status": CHECK_OK, "detail": "redis ping ok"}
        if broker.startswith("amqp://") or broker.startswith("amqps://"):
            from celery import current_app  # type: ignore

            with current_app.connection_or_acquire(connect_timeout=2) as conn:
                conn.ensure_connection(max_retries=0)
            return {"status": CHECK_OK, "detail": "rabbitmq connect ok"}
        return {"status": CHECK_DEGRADED, "detail": "unsupported broker scheme"}
    except Exception as exc:  # noqa: BLE001
        return {"status": CHECK_DEGRADED, "detail": f"{type(exc).__name__}: {exc}"}


def _check_index_freshness() -> dict:
    """Worst-of per-workspace coverage. We pick the lowest coverage
    across enabled workspaces — the operator wants to know if ANY
    workspace's index is lagging, not the average."""
    try:
        from ai.metrics import _index_coverage_samples

        samples = _index_coverage_samples()
        if not samples:
            return {"status": CHECK_OK, "detail": "no ai-enabled workspaces"}
        worst = min(samples, key=lambda s: s[1])
        labels, cov = worst
        if cov < INDEX_HEALTH_COVERAGE:
            return {
                "status": CHECK_DEGRADED,
                "detail": (
                    f"workspace {labels.get('workspace_id', '?')}/"
                    f"{labels.get('source_type', '?')} coverage {cov:.2f}"
                ),
            }
        return {"status": CHECK_OK, "detail": f"min coverage {cov:.2f}"}
    except Exception as exc:  # noqa: BLE001
        return {"status": CHECK_DEGRADED, "detail": f"{type(exc).__name__}: {exc}"}


def _check_budget_headroom() -> dict:
    """Aggregate budget probe. If ANY workspace is over
    :data:`BUDGET_HEALTH_RATIO` of its monthly budget, flag degraded.
    Past 1.0 the budget guard hard-blocks AI requests — that's still
    "degraded" from a system-health perspective, not "down" (other
    workspaces unaffected)."""
    try:
        from ai.metrics import _budget_samples

        worst_ws = None
        worst_ratio = 0.0
        for labels, value in _budget_samples():
            if labels.get("metric") == "ratio" and value > worst_ratio:
                worst_ratio = value
                worst_ws = labels.get("workspace_id", "?")
        if worst_ws is None:
            return {"status": CHECK_OK, "detail": "no ai-enabled workspaces"}
        if worst_ratio >= 1.0:
            return {
                "status": CHECK_DEGRADED,
                "detail": f"workspace {worst_ws} over budget ({worst_ratio:.2f})",
            }
        if worst_ratio >= BUDGET_HEALTH_RATIO:
            return {
                "status": CHECK_DEGRADED,
                "detail": f"workspace {worst_ws} near budget ({worst_ratio:.2f})",
            }
        return {"status": CHECK_OK, "detail": f"max ratio {worst_ratio:.2f}"}
    except Exception as exc:  # noqa: BLE001
        return {"status": CHECK_DEGRADED, "detail": f"{type(exc).__name__}: {exc}"}


def _rollup(checks: dict[str, dict]) -> tuple[str, int]:
    """Compose the per-check map into an overall status + HTTP code.

    Critical-down checks: ``database``, ``vector_ext``. If either of
    those is ``down``, the whole layer is unusable and we return 503.
    Anything else flags ``degraded`` but the listener stays 200 so a
    load balancer keeps probing rather than yanking the instance.
    """
    critical = ("database", "vector_ext")
    if any(checks[k]["status"] == CHECK_DOWN for k in critical if k in checks):
        return "down", http_status.HTTP_503_SERVICE_UNAVAILABLE
    if any(c["status"] != CHECK_OK for c in checks.values()):
        return "degraded", http_status.HTTP_200_OK
    return "ok", http_status.HTTP_200_OK


class HealthView(APIView):
    """``GET /api/ai/health/``.

    Open — same rationale as :class:`ai.metrics.MetricsView`: operator
    infra calls this without a Plane user context. Unlike metrics, we
    do NOT gate it on a token: deploy probes need a single
    well-known URL with no extra config. The endpoint reveals only
    aggregate status, no per-workspace data.
    """

    permission_classes = [AllowAny]

    def get(self, request):
        # Short-circuit: if the DB is down, the other checks can't run
        # meaningfully (they all need the connection).
        db = _check_database()
        if db["status"] == CHECK_DOWN:
            return Response(
                {
                    "status": "down",
                    "checks": {"database": db},
                },
                status=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        checks = {
            "database": db,
            "vector_ext": _check_vector_extension(),
            "broker": _check_broker(),
            "index_freshness": _check_index_freshness(),
            "budget": _check_budget_headroom(),
        }
        rollup, code = _rollup(checks)
        return Response({"status": rollup, "checks": checks}, status=code)


__all__ = [
    "HealthView",
    "INDEX_HEALTH_COVERAGE",
    "BUDGET_HEALTH_RATIO",
]
