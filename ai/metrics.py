"""TZ 6.2 — Prometheus exposition for the AI layer.

We hand-roll the OpenMetrics exposition format here rather than pull
in ``prometheus_client``. Three reasons:

  1. The format is trivially simple — a couple of `# HELP` / `# TYPE`
     lines per metric and one numeric line per labelled time series.
     A 200-line module covers everything we expose.
  2. Adding a Python dep to the API image (with its own C-deps for
     multiprocess mode) is a maintenance liability we don't need for
     this volume of metrics.
  3. Our counters are best aggregated server-side at scrape time
     anyway — :func:`tokens_used_this_month` is a SQL aggregate
     against ``AIUsageLog``, and re-shaping that as a
     ``prometheus_client.Counter`` would duplicate state. Keeping the
     metrics endpoint a pure SQL-projection means a Prometheus scrape
     can never disagree with the underlying audit tables.

In-process counters (provider errors, agent actions) live in a
:class:`_Counter` registry. Counters reset on process restart — that
matches Prometheus' own counter-reset semantics; Alertmanager rules
use ``increase()`` / ``rate()`` which tolerate resets.

Endpoint shape::

    GET /api/ai/metrics/

Auth: workspace-membership is irrelevant — this is operator-facing
infrastructure. Locked down by an ``X-Metrics-Token`` header
matching ``settings.PLANEAI_METRICS_TOKEN`` (env-injected). Prometheus
sets the header via the ``authorization`` field in its scrape config.
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from datetime import timedelta
from typing import Iterable

from django.apps import apps as django_apps
from django.conf import settings
from django.db.models import Sum
from django.http import HttpResponse
from django.utils import timezone
from rest_framework.permissions import AllowAny
from rest_framework.views import APIView

from ai.models import AIAgentActionLog, AIUsageLog, DocumentChunk, WorkspaceAIConfig


logger = logging.getLogger("plane.ai.metrics")


# ---------------------------------------------------------------------------
# In-process counters
# ---------------------------------------------------------------------------


class _Counter:
    """Tiny labelled counter with a lock — multi-thread Django (gunicorn
    workers handle one request at a time, but middlewares + Celery may
    increment from different threads in the same process).

    Not a Prometheus client; just a thread-safe ``defaultdict`` with a
    rendering helper. Resets on process restart, which is fine — see
    module docstring.
    """

    def __init__(self, name: str, help_text: str, label_names: tuple[str, ...]) -> None:
        self.name = name
        self.help_text = help_text
        self.label_names = label_names
        self._values: dict[tuple[str, ...], float] = defaultdict(float)
        self._lock = threading.Lock()

    def inc(self, labels: dict[str, str], amount: float = 1.0) -> None:
        """Increment one labelled time-series. Unknown labels are
        ignored silently so a code site can pass an extra field
        without breaking the scrape."""
        key = tuple(str(labels.get(n, "")) for n in self.label_names)
        with self._lock:
            self._values[key] += amount

    def render(self) -> str:
        lines = [
            f"# HELP {self.name} {self.help_text}",
            f"# TYPE {self.name} counter",
        ]
        with self._lock:
            items = list(self._values.items())
        for key, val in items:
            labels = ",".join(
                f'{n}="{_escape(v)}"' for n, v in zip(self.label_names, key)
            )
            lines.append(f"{self.name}{{{labels}}} {val}")
        return "\n".join(lines)


def _escape(s: str) -> str:
    """Prometheus label-value escape: backslash, double-quote, newline."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


# These two are imported by ai/providers.py and ai/agent_worker.py to
# bump counters at the right moments. Keeping them as module-level
# singletons means every Django worker shares one in-process table.
PROVIDER_ERRORS = _Counter(
    name="planeai_provider_errors_total",
    help_text="Anthropic/OpenAI errors observed by the API process, by status class.",
    label_names=("provider", "kind"),
)

AGENT_ACTIONS = _Counter(
    name="planeai_agent_actions_total",
    help_text="Agent action attempts dispatched, by tool and outcome.",
    label_names=("workspace_id", "tool_name", "status"),
)


# ---------------------------------------------------------------------------
# DB-projection helpers (gauges)
# ---------------------------------------------------------------------------


def _month_start():
    return timezone.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _gauge_lines(name: str, help_text: str, samples: Iterable[tuple[dict[str, str], float]]) -> list[str]:
    """Render ``samples = [(label_dict, value), ...]`` as a gauge.

    Two header lines (``# HELP`` / ``# TYPE``) followed by one line
    per series. Empty samples still emit the headers — Grafana
    queries are happier with "metric exists, zero series" than with
    "metric absent".
    """
    lines = [
        f"# HELP {name} {help_text}",
        f"# TYPE {name} gauge",
    ]
    for labels, value in samples:
        body = ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items())
        lines.append(f"{name}{{{body}}} {value}")
    return lines


def _budget_samples() -> list[tuple[dict[str, str], float]]:
    """Per-workspace ``(used, budget, ratio)`` aggregated from
    ``AIUsageLog`` for the current calendar month.

    One scrape ⇒ one SQL aggregate per workspace. ``cache_read_tokens``
    is excluded from the "used" total to mirror
    :func:`ai.usage.tokens_used_this_month` — cache reads are
    discounts on fresh inputs, not new spend.
    """
    start = _month_start()
    samples: list[tuple[dict[str, str], float]] = []

    cfgs = list(
        WorkspaceAIConfig.objects.filter(enabled=True).values(
            "workspace_id", "monthly_token_budget"
        )
    )
    if not cfgs:
        return samples

    ws_ids = [c["workspace_id"] for c in cfgs]
    used_by_ws = {
        row["workspace_id"]: (row["i"] or 0) + (row["o"] or 0) + (row["c"] or 0)
        for row in AIUsageLog.objects.filter(
            workspace_id__in=ws_ids, created_at__gte=start
        )
        .values("workspace_id")
        .annotate(i=Sum("input_tokens"), o=Sum("output_tokens"), c=Sum("cache_creation_tokens"))
    }

    for c in cfgs:
        ws = str(c["workspace_id"])
        used = used_by_ws.get(c["workspace_id"], 0)
        budget = int(c["monthly_token_budget"] or 0)
        ratio = (used / budget) if budget else 0.0
        samples.append(({"workspace_id": ws, "metric": "used"}, float(used)))
        samples.append(({"workspace_id": ws, "metric": "budget"}, float(budget)))
        samples.append(({"workspace_id": ws, "metric": "ratio"}, float(ratio)))
    return samples


def _index_coverage_samples() -> list[tuple[dict[str, str], float]]:
    """Per-workspace, per-source-type indexing coverage 0..1.

    Same SQL shape as :func:`ai.views._coverage_breakdown` but
    re-aggregated here so the metrics endpoint doesn't need an HTTP
    round-trip to the search view. Anything below ~0.9 means
    "новые задачи не индексируются" — that's the "дрейф индекса"
    alert from TZ 6.2.
    """
    Issue = django_apps.get_model("db", "Issue")
    IssueComment = django_apps.get_model("db", "IssueComment")
    Page = django_apps.get_model("db", "Page")

    samples: list[tuple[dict[str, str], float]] = []

    # Set of workspaces we care about: those with AI enabled. A ws
    # without an AIConfig has zero AI activity so its coverage doesn't
    # matter — keeps the metric cardinality bounded.
    workspaces = list(
        WorkspaceAIConfig.objects.filter(enabled=True).values_list(
            "workspace_id", flat=True
        )
    )
    if not workspaces:
        return samples

    # Totals per ws/source — one query per source kind, grouped by ws.
    from django.db.models import Count

    totals_work = dict(
        Issue.objects.filter(
            workspace_id__in=workspaces,
            deleted_at__isnull=True,
            is_draft=False,
        )
        .values("workspace_id")
        .annotate(n=Count("id"))
        .values_list("workspace_id", "n")
    )
    totals_comment = dict(
        IssueComment.objects.filter(
            workspace_id__in=workspaces, deleted_at__isnull=True
        )
        .values("workspace_id")
        .annotate(n=Count("id"))
        .values_list("workspace_id", "n")
    )
    totals_page = dict(
        Page.objects.filter(
            workspace_id__in=workspaces,
            deleted_at__isnull=True,
            archived_at__isnull=True,
        )
        .values("workspace_id")
        .annotate(n=Count("id"))
        .values_list("workspace_id", "n")
    )

    # Indexed = distinct (source_type, source_id) in ``DocumentChunk``.
    indexed = (
        DocumentChunk.objects.filter(workspace_id__in=workspaces)
        .values("workspace_id", "source_type")
        .annotate(n=Count("source_id", distinct=True))
    )
    indexed_by_ws_src: dict[tuple, int] = defaultdict(int)
    for r in indexed:
        indexed_by_ws_src[(r["workspace_id"], r["source_type"])] = r["n"]

    for ws in workspaces:
        for source_type, totals in (
            ("work_item", totals_work),
            ("comment", totals_comment),
            ("page", totals_page),
        ):
            total = totals.get(ws, 0)
            cov = (
                indexed_by_ws_src.get((ws, source_type), 0) / total
                if total
                else 1.0
            )
            samples.append(
                (
                    {"workspace_id": str(ws), "source_type": source_type},
                    float(round(cov, 4)),
                )
            )
    return samples


def _agent_activity_samples() -> list[tuple[dict[str, str], float]]:
    """Per-workspace applied agent actions in the last 5 minutes.

    The "петля агента" alert from TZ 6.2 hooks this gauge:
    AGENT_MAX_ACTIONS (5) per worker run × typical fire rate gives a
    natural ceiling. Sustained > 30/5m in a workspace = anomaly.
    """
    five_min_ago = timezone.now() - timedelta(minutes=5)
    samples: list[tuple[dict[str, str], float]] = []
    rows = (
        AIAgentActionLog.objects.filter(
            created_at__gte=five_min_ago,
            status=AIAgentActionLog.STATUS_APPLIED,
        )
        .values("workspace_id")
        .annotate(n=__count__())
    )
    for r in rows:
        samples.append(
            ({"workspace_id": str(r["workspace_id"])}, float(r["n"]))
        )
    return samples


def __count__():
    # Local helper — keeps the .annotate() readable. Wrapper for
    # ``Count("id")`` so the imports up top stay narrow.
    from django.db.models import Count

    return Count("id")


def _celery_queue_samples() -> list[tuple[dict[str, str], float]]:
    """Best-effort queue length from the broker.

    We do NOT make the metrics endpoint depend on a healthy broker —
    if Redis/RabbitMQ is unreachable the scrape still returns 200 with
    other metrics; the queue gauge becomes a single error-marker
    series so the operator can see "broker probe failed" without the
    whole metrics pipeline going dark.
    """
    try:
        from django.conf import settings as dj_settings
        broker = dj_settings.CELERY_BROKER_URL
    except Exception:
        return [({"queue": "unknown", "error": "no_broker_url"}, 0.0)]

    queue_names = getattr(settings, "PLANEAI_CELERY_QUEUES", ("celery",))
    samples: list[tuple[dict[str, str], float]] = []
    try:
        if broker.startswith("redis://") or broker.startswith("rediss://"):
            import redis  # type: ignore

            r = redis.Redis.from_url(broker)
            for q in queue_names:
                samples.append(({"queue": q}, float(r.llen(q))))
        elif broker.startswith("amqp://") or broker.startswith("amqps://"):
            # rabbitmq: use kombu via the existing celery app. The
            # cheapest probe is to look up the queue and ask its size.
            from celery import current_app  # type: ignore

            with current_app.connection_or_acquire() as conn:
                for q in queue_names:
                    try:
                        _, n, _ = conn.default_channel.queue_declare(
                            queue=q, passive=True
                        )
                        samples.append(({"queue": q}, float(n)))
                    except Exception:
                        samples.append(({"queue": q, "error": "declare_failed"}, 0.0))
        else:
            samples.append(({"queue": "unknown", "error": "unsupported_broker"}, 0.0))
    except Exception as exc:  # noqa: BLE001
        logger.warning("celery queue probe failed: %s", exc)
        samples.append(({"queue": "unknown", "error": "probe_failed"}, 0.0))
    return samples


# ---------------------------------------------------------------------------
# View
# ---------------------------------------------------------------------------


class MetricsView(APIView):
    """``GET /api/ai/metrics/``.

    Returns the full OpenMetrics text for the AI layer. Auth via a
    static ``X-Metrics-Token`` header — Prometheus passes it in its
    scrape config (``bearer_token`` or ``authorization`` header
    rewrites work too). Returns 401 if absent or mismatched, so the
    endpoint can't be probed by random scanners.

    Why not ``IsAuthenticated``: Prometheus has no Plane user. The
    endpoint exposes aggregate counts that the operator already has
    DB access to — the token is enough.
    """

    permission_classes = [AllowAny]

    def get(self, request):
        token = (
            request.headers.get("X-Metrics-Token")
            or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        )
        expected = getattr(settings, "PLANEAI_METRICS_TOKEN", "")
        if not expected:
            return HttpResponse(
                "metrics disabled (PLANEAI_METRICS_TOKEN unset)\n",
                status=503,
                content_type="text/plain; charset=utf-8",
            )
        if token != expected:
            return HttpResponse(
                "unauthorized\n",
                status=401,
                content_type="text/plain; charset=utf-8",
            )

        lines: list[str] = []

        # Counters first — they hold static metadata so a freshly-
        # restarted process exposes the metric even with zero samples.
        lines.append(PROVIDER_ERRORS.render())
        lines.append(AGENT_ACTIONS.render())

        # Budget — the most important gauge per TZ 6.2's "финансовый
        # предохранитель". Three samples per workspace: used / budget
        # / ratio. Prometheus alerts on ``ratio > 0.8``.
        lines.extend(
            _gauge_lines(
                "planeai_workspace_tokens",
                "Per-workspace token totals for the current calendar month.",
                _budget_samples(),
            )
        )

        # Index coverage — drives the index-drift alert.
        lines.extend(
            _gauge_lines(
                "planeai_index_coverage",
                "Indexing coverage 0..1 per workspace and source type.",
                _index_coverage_samples(),
            )
        )

        # Agent activity in the last 5 minutes — drives the loop alert.
        lines.extend(
            _gauge_lines(
                "planeai_agent_actions_5m",
                "Applied agent actions in the last 5 minutes per workspace.",
                _agent_activity_samples(),
            )
        )

        # Celery queue length — drives the backfill-stuck alert.
        lines.extend(
            _gauge_lines(
                "planeai_celery_queue_length",
                "Approximate queue length per Celery queue (best effort).",
                _celery_queue_samples(),
            )
        )

        body = "\n".join(lines) + "\n"
        return HttpResponse(body, content_type="text/plain; version=0.0.4; charset=utf-8")


__all__ = [
    "PROVIDER_ERRORS",
    "AGENT_ACTIONS",
    "MetricsView",
]
