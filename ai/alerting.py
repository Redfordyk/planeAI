"""TZ 6.2 — alert webhook receiver.

Alertmanager's webhook receiver fires a JSON payload at this view;
we shape it into a short human-readable message and POST it onward
to the team's chat (Slack incoming webhook / Telegram bot API /
Discord webhook / anything that accepts a JSON POST).

Two design choices to keep this simple:

  1. **Vendor-agnostic forwarding.** ``ALERT_WEBHOOK_URL`` (and an
     optional ``ALERT_WEBHOOK_FORMAT`` of ``slack`` / ``telegram`` /
     ``discord`` / ``raw``) picks the shape. No SDK dependency for
     any of them — they all accept plain JSON.

  2. **Auth via shared secret in the URL path or header.** Alertmanager
     can include any static auth (basic auth, bearer); we accept a
     ``X-Alert-Token`` header matching ``ALERT_WEBHOOK_TOKEN``. Same
     pattern as the metrics endpoint.

The view is intentionally minimal — Alertmanager itself handles
deduplication, grouping, inhibitions. We only translate one alert
batch into one chat message.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import urllib.error
import urllib.request

from django.conf import settings
from rest_framework import status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView


logger = logging.getLogger("plane.ai.alerting")


# A short title for each alertname we know about — matches the rule
# files in deploy-local/monitoring/alerts.yml. Anything not in this
# map renders with the alertname verbatim.
ALERT_TITLES: dict[str, str] = {
    "PlaneAIBudgetWarning": "💸 Расход токенов > 80% месячного лимита",
    "PlaneAIBackfillStuck": "⛔ Очередь Celery растёт — бэкафилл застрял",
    "PlaneAIIndexDrift": "🧭 Индекс отстаёт от данных",
    "PlaneAIProviderErrors": "🔌 Ошибки провайдера LLM",
    "PlaneAIAgentLoop": "🔁 Аномальная активность агента (возможна петля)",
}


def _format_for(fmt: str, title: str, body: str) -> tuple[str, dict[str, str]]:
    """Render the title+body for the requested chat vendor. Returns
    (json-payload, extra-headers)."""
    if fmt == "slack":
        # Slack incoming-webhook accepts {"text": "..."} or a blocks[]
        # array. We use plain text — works for the simplest webhook
        # URL the team can paste in.
        return json.dumps({"text": f"*{title}*\n{body}"}), {
            "Content-Type": "application/json"
        }
    if fmt == "discord":
        return json.dumps({"content": f"**{title}**\n{body}"}), {
            "Content-Type": "application/json"
        }
    if fmt == "telegram":
        # Telegram's sendMessage. The URL itself includes the bot
        # token and chat_id (configured by operator) — we just POST
        # the text.
        return json.dumps({"text": f"*{title}*\n{body}", "parse_mode": "Markdown"}), {
            "Content-Type": "application/json"
        }
    # Default: raw — let the destination figure it out.
    return json.dumps({"title": title, "body": body}), {
        "Content-Type": "application/json"
    }


def _summarise_payload(payload: dict[str, Any]) -> tuple[str, str]:
    """Turn an Alertmanager webhook payload into ``(title, body)``.

    Alertmanager's shape::

        {
          "status": "firing"|"resolved",
          "alerts": [{
              "labels":     {"alertname": "...", "severity": "...", ...},
              "annotations": {"summary": "...", "description": "..."},
              "startsAt":    "...",
              "generatorURL":"..."
          }, ...],
          ...
        }

    We pick the first alert as the canonical (Alertmanager groups
    similar alerts before firing the webhook) and append a count if
    multiple.
    """
    alerts = payload.get("alerts") or []
    if not alerts:
        return "PlaneAI alert", json.dumps(payload)[:300]

    first = alerts[0]
    labels = first.get("labels", {}) or {}
    annotations = first.get("annotations", {}) or {}
    alertname = labels.get("alertname", "Unknown")
    status_word = payload.get("status", "firing")
    title_prefix = ALERT_TITLES.get(alertname, alertname)
    title = f"[{status_word.upper()}] {title_prefix}"
    body_parts: list[str] = []
    if annotations.get("summary"):
        body_parts.append(str(annotations["summary"]))
    if annotations.get("description"):
        body_parts.append(str(annotations["description"]))
    # Surface the most useful labels — workspace_id is the one
    # responders need first ("which workspace burned the budget?").
    ws = labels.get("workspace_id")
    if ws:
        body_parts.append(f"workspace: `{ws}`")
    if len(alerts) > 1:
        body_parts.append(f"(+{len(alerts) - 1} similar)")
    body = "\n".join(body_parts) or "no annotations"
    return title, body


class AlertWebhookView(APIView):
    """``POST /api/ai/alerts/webhook/``.

    Receives Alertmanager payloads, forwards to the team chat.

    Returns 204 on success, 500 on forward failure (so Alertmanager
    retries — its default exponential backoff is fine). 401 on
    missing/wrong token.
    """

    permission_classes = [AllowAny]

    def post(self, request):
        token = request.headers.get("X-Alert-Token", "")
        expected = getattr(settings, "ALERT_WEBHOOK_TOKEN", "")
        if not expected:
            return Response(
                {"error": "alert webhook disabled (ALERT_WEBHOOK_TOKEN unset)"},
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        if token != expected:
            return Response(
                {"error": "unauthorized"}, status=status.HTTP_401_UNAUTHORIZED
            )

        forward_url = getattr(settings, "ALERT_WEBHOOK_URL", "")
        if not forward_url:
            logger.warning("alert received but ALERT_WEBHOOK_URL not set — dropping")
            return Response(
                {"warning": "no forward URL configured"},
                status=status.HTTP_204_NO_CONTENT,
            )

        title, body = _summarise_payload(request.data or {})
        fmt = getattr(settings, "ALERT_WEBHOOK_FORMAT", "raw")
        payload, headers = _format_for(fmt, title, body)

        try:
            req = urllib.request.Request(
                forward_url, data=payload.encode("utf-8"), headers=headers
            )
            with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
                resp.read()
        except urllib.error.URLError as exc:
            logger.error("alert forward failed: %s", exc)
            return Response(
                {"error": f"forward failed: {exc}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

        return Response(status=status.HTTP_204_NO_CONTENT)


__all__ = [
    "AlertWebhookView",
    "ALERT_TITLES",
]
