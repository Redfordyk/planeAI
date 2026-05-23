"""TZ 6.8 — rollout status snapshot.

The TZ asks for the PM to "actively monitor the first 3-5 days".
This command compresses the 4-5 dashboards/Grafana/Alertmanager
tabs the PM would otherwise watch into a single shell line they
can run from their laptop.

For each enabled workspace, prints:

  - enabled / kill-switch state
  - calls / billable tokens / USD spent in the last 24h
  - month-to-date spend + ratio against budget (matches BUDGET.md)
  - the most recent applied agent action (if any) — sanity check
    that the agent didn't go silent or runaway
  - latest error-tagged usage row, if any

The intent is "one paragraph the PM pastes into the Slack thread
every morning during the rollout". No live re-render, no curses
UI — just a snapshot.

Usage::

    python manage.py rollout_status
    python manage.py rollout_status --workspace <id-or-slug>
    python manage.py rollout_status --since 24       # hours
    python manage.py rollout_status --json
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Sum
from django.utils import timezone


logger = logging.getLogger("plane.ai.rollout")


@dataclass
class WorkspaceStatus:
    workspace_id: str
    workspace_slug: str
    enabled: bool
    calls_24h: int
    billable_tokens_24h: int
    cost_usd_24h: str
    cost_usd_month: str
    budget_used_ratio: float
    last_agent_action: dict | None
    agent_actions_24h: dict[str, int]
    notes: list[str]


class Command(BaseCommand):
    help = (
        "Snapshot of rollout state across all AI-enabled workspaces. "
        "Designed to be run by the PM each morning during the first "
        "3-5 days after release. See RELEASE.md for the daily "
        "checklist."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--workspace",
            help=(
                "Optional workspace scope (UUID or slug). Without it, "
                "all WorkspaceAIConfig rows are listed."
            ),
        )
        parser.add_argument(
            "--since",
            type=int,
            default=24,
            help="How many hours of activity to summarise (default 24).",
        )
        parser.add_argument(
            "--json",
            dest="as_json",
            action="store_true",
            help="Emit machine-readable JSON (for piping into automation).",
        )

    # ------------------------------------------------------------------

    def handle(self, *args, **opts):
        ws_filter = self._resolve_workspace(opts.get("workspace"))
        hours = max(1, int(opts["since"]))
        since = timezone.now() - timedelta(hours=hours)

        # Late imports — match the rest of the management modules.
        from ai.models import (
            AIAgentActionLog,
            AIUsageLog,
            WorkspaceAIConfig,
        )
        from ai.usage import budget_status

        cfgs = WorkspaceAIConfig.objects.select_related("workspace").all()
        if ws_filter is not None:
            cfgs = cfgs.filter(workspace_id=ws_filter)
        cfgs = list(cfgs)

        statuses: list[WorkspaceStatus] = []
        for cfg in cfgs:
            ws_id = cfg.workspace_id
            slug = getattr(cfg.workspace, "slug", str(ws_id))

            # --- usage in the last N hours ---
            since_qs = AIUsageLog.objects.filter(
                workspace_id=ws_id, created_at__gte=since
            )
            agg = since_qs.aggregate(
                calls=Count("id"),
                inp=Sum("input_tokens"),
                out=Sum("output_tokens"),
                cc=Sum("cache_creation_tokens"),
                cost=Sum("cost_usd"),
            )
            billable = (
                (agg["inp"] or 0)
                + (agg["out"] or 0)
                + (agg["cc"] or 0)
            )
            cost_24h = agg["cost"] or Decimal("0")

            # --- month-to-date vs budget ---
            used, budget, _ = budget_status(ws_id)
            ratio = (used / budget) if budget else 0.0

            # Approximate month-to-date USD — sum cost_usd over current
            # month. Cheaper than computing in usage.py for now.
            month_start = timezone.now().replace(
                day=1, hour=0, minute=0, second=0, microsecond=0
            )
            cost_month = (
                AIUsageLog.objects.filter(
                    workspace_id=ws_id, created_at__gte=month_start
                )
                .aggregate(c=Sum("cost_usd"))["c"]
                or Decimal("0")
            )

            # --- agent activity ---
            agent_qs = AIAgentActionLog.objects.filter(
                workspace_id=ws_id, created_at__gte=since
            )
            agent_status_counts = dict(
                agent_qs.values_list("status").annotate(n=Count("id"))
            )
            # ``values_list("status")`` returns tuples (status,) — turn
            # the dict into a flat str→int.
            agent_actions_24h = {
                str(k[0] if isinstance(k, tuple) else k): int(v)
                for k, v in agent_status_counts.items()
            }
            last_action = (
                AIAgentActionLog.objects.filter(workspace_id=ws_id)
                .order_by("-created_at")
                .first()
            )
            last_action_payload: dict | None = None
            if last_action is not None:
                last_action_payload = {
                    "tool_name": last_action.tool_name,
                    "status": last_action.status,
                    "issue_id": str(last_action.issue_id),
                    "created_at": last_action.created_at.isoformat(),
                }

            # --- notes (operator-readable alerts) ---
            notes: list[str] = []
            if not cfg.enabled:
                notes.append("AI DISABLED (kill switch on)")
            if budget and ratio >= 0.80:
                notes.append(
                    f"budget ratio {ratio:.0%} — see BUDGET.md / TZ 6.2 alert"
                )
            if (
                cfg.enabled
                and agent_actions_24h.get("rejected", 0) > 5
                and agent_actions_24h.get("rejected", 0)
                > agent_actions_24h.get("applied", 0)
            ):
                # More rejections than successes is a sign the agent
                # is misbehaving in a way the white-list catches.
                notes.append(
                    f"agent: more rejected ({agent_actions_24h['rejected']}) "
                    f"than applied ({agent_actions_24h.get('applied', 0)}) — "
                    "model may be off-track"
                )
            # Apparent silence on an enabled workspace — possible
            # ingest signal regression (see RUNBOOK troubleshooting).
            if cfg.enabled and agg["calls"] == 0 and hours >= 24:
                notes.append(
                    "no AI calls in the last 24h — check if features "
                    "are unreachable or if no one is using them yet"
                )

            statuses.append(
                WorkspaceStatus(
                    workspace_id=str(ws_id),
                    workspace_slug=slug,
                    enabled=cfg.enabled,
                    calls_24h=int(agg["calls"] or 0),
                    billable_tokens_24h=int(billable),
                    cost_usd_24h=str(cost_24h),
                    cost_usd_month=str(cost_month),
                    budget_used_ratio=round(ratio, 4),
                    last_agent_action=last_action_payload,
                    agent_actions_24h=agent_actions_24h,
                    notes=notes,
                )
            )

        if opts["as_json"]:
            self.stdout.write(
                json.dumps(
                    {
                        "since_hours": hours,
                        "generated_at": timezone.now().isoformat(),
                        "workspaces": [asdict(s) for s in statuses],
                    },
                    ensure_ascii=False,
                )
            )
        else:
            self._render_human(statuses, hours=hours)

    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_workspace(token):
        if not token:
            return None
        from django.apps import apps as django_apps

        Workspace = django_apps.get_model("db", "Workspace")
        try:
            uid = uuid.UUID(str(token))
            row = Workspace.objects.filter(id=uid).only("id").first()
            if row:
                return row.id
        except (ValueError, AttributeError):
            pass
        row = Workspace.objects.filter(slug=token).only("id").first()
        if not row:
            raise CommandError(
                f"workspace {token!r} not found (checked UUID and slug)"
            )
        return row.id

    # ------------------------------------------------------------------

    def _render_human(self, statuses: list[WorkspaceStatus], *, hours: int):
        if not statuses:
            self.stdout.write(
                self.style.WARNING("no WorkspaceAIConfig rows found.")
            )
            return
        self.stdout.write(
            f"rollout_status — last {hours}h, generated {timezone.now().isoformat()}"
        )
        for s in statuses:
            label = (
                self.style.SUCCESS("[enabled]")
                if s.enabled
                else self.style.WARNING("[disabled]")
            )
            self.stdout.write(
                f"\n{label} {s.workspace_slug}  ({s.workspace_id})"
            )
            self.stdout.write(
                f"  usage {hours}h:   calls={s.calls_24h}, "
                f"billable_tokens={s.billable_tokens_24h:,}, "
                f"cost=${s.cost_usd_24h}"
            )
            self.stdout.write(
                f"  month so far:    cost=${s.cost_usd_month}, "
                f"budget ratio={s.budget_used_ratio:.0%}"
            )
            agent_line = ", ".join(
                f"{k}={v}" for k, v in sorted(s.agent_actions_24h.items())
            ) or "—"
            self.stdout.write(f"  agent {hours}h:   {agent_line}")
            if s.last_agent_action:
                la = s.last_agent_action
                self.stdout.write(
                    f"  last agent action: {la['tool_name']}/{la['status']} "
                    f"on {la['issue_id']} at {la['created_at']}"
                )
            for note in s.notes:
                self.stdout.write(self.style.WARNING(f"  ! {note}"))
