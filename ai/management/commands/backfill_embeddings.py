"""Bulk-enqueue ``reindex_source`` for every existing object in a workspace.

Used once when AI is first enabled on a workspace that already has
data (the runtime ingest hooks in TZ 1.4 only catch new writes).

Throttling: each task gets `countdown = n // rate` seconds, where
`n` is the task ordinal. With `--rate 3`, three tasks fire per second
target. This keeps OpenAI request rate well under the 1k req/min
ceiling for typical embedding workloads and avoids saturating the
Celery worker queue on cold start.

Re-runs are safe: ``reindex_source`` short-circuits when the content
hash already matches the stored chunk's hash. So if a backfill
crashes halfway, re-running picks up where it left off without
double-paying for embedding tokens.

DPA caveat (see [GDPR.md](../../../GDPR.md)): running this against
real customer data sends every issue/comment/page text to OpenAI.
On prod, this is gated behind the production backfill checklist
(TZ 6.6). On staging it's fine — synthetic data only.

TZ 6.6 changes — the production acceptance gate:

  - ``--i-confirm-dpa-closed`` is now required on any non-dry-run.
    The flag exists so an operator can't paste the staging command
    on a prod host by reflex and accidentally ship customer text
    to OpenAI without the legal basis in place.
  - ``--dry-run`` now also estimates the **embedding cost in USD**
    using ``ai.pricing.embed_cost``, so the PM sees the bill BEFORE
    the bill arrives. We use the same content-length-to-token
    heuristic OpenAI itself documents (~4 chars per token); precise
    accounting happens row-by-row in ``ai.tasks.reindex_source``.
  - ``--verbose`` prints the per-project breakdown so the operator
    can confirm that projects flagged ``exclude_from_ai`` are in
    fact being skipped.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Iterable

from django.core.management.base import BaseCommand, CommandError
from django.db.models import QuerySet

from ai.models import AIProjectSettings, DocumentChunk, WorkspaceAIConfig
from ai.pricing import embed_cost
from ai.tasks import reindex_source


logger = logging.getLogger("plane.ai.backfill")


SOURCES = ("work_item", "comment", "page")


# OpenAI's documented heuristic for English text: ~4 characters per
# token. text-embedding-3-small has the same tokenizer as gpt-4o, so
# the same rule applies. This is intentionally approximate (it's a
# pre-spend estimate, not billing) — actual token counts come from
# the OpenAI response and are written into ``AIUsageLog`` per chunk.
CHARS_PER_TOKEN: float = 4.0


# Default embed model — must match WorkspaceAIConfig.embed_model
# default and ai.providers.EMBED_MODEL. If the config has a different
# model, we read it from there in handle().
DEFAULT_EMBED_MODEL = "text-embedding-3-small"


class Command(BaseCommand):
    help = "Enqueue ai.reindex_source for every Issue/IssueComment/Page in a workspace."

    def add_arguments(self, parser):
        parser.add_argument(
            "--workspace",
            required=True,
            help="Workspace UUID (the value of db.Workspace.id).",
        )
        parser.add_argument(
            "--rate",
            type=int,
            default=3,
            help="Target enqueue rate per second (tasks get countdown = n // rate).",
        )
        parser.add_argument(
            "--source",
            choices=SOURCES + ("all",),
            default="all",
            help="Limit to a single source type (default: all).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help=(
                "Count what would be enqueued; do not actually enqueue. "
                "Also prints a USD cost estimate based on content length."
            ),
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Enqueue even if WorkspaceAIConfig.enabled is False.",
        )
        parser.add_argument(
            "--i-confirm-dpa-closed",
            action="store_true",
            help=(
                "TZ 6.6 production gate — required on any non-dry-run. "
                "Acknowledges that the DPA (TZ 0.7) is signed and that "
                "projects requiring exclusion are flagged via "
                "AIProjectSettings.exclude_from_ai (TZ 3.4). Without "
                "this flag the command refuses to send real text to "
                "OpenAI."
            ),
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help=(
                "Print per-project breakdown including projects skipped "
                "via exclude_from_ai. Useful as the final verification "
                "before a production backfill."
            ),
        )

    def handle(self, *args, **opts):
        from django.apps import apps as django_apps

        Issue = django_apps.get_model("db", "Issue")
        IssueComment = django_apps.get_model("db", "IssueComment")
        Page = django_apps.get_model("db", "Page")

        ws_id = opts["workspace"]
        rate = max(1, opts["rate"])
        only_source = opts["source"]
        dry_run = opts["dry_run"]
        force = opts["force"]
        verbose = opts["verbose"]
        dpa_ack = opts["i_confirm_dpa_closed"]

        # ---- TZ 6.6 production gate ------------------------------
        # The DPA flag is required on any **real** backfill. Dry-runs
        # don't touch OpenAI so they don't need the ack. We refuse
        # silently-typed real runs to make sure prod operators see
        # the legal-basis prompt every time.
        if not dry_run and not dpa_ack:
            raise CommandError(
                "Refusing to start a real backfill without "
                "--i-confirm-dpa-closed. Either run with --dry-run to "
                "preview, or, after confirming the DPA (TZ 0.7) is "
                "signed and AIProjectSettings.exclude_from_ai (TZ 3.4) "
                "is applied to private projects, pass the flag."
            )

        # Workspace gate. The hot path (signals) checks this too, but
        # backfill is operator-driven — surface the misconfiguration
        # loudly instead of silently doing nothing.
        cfg = WorkspaceAIConfig.objects.filter(workspace_id=ws_id).first()
        if cfg is None:
            raise CommandError(
                f"workspace {ws_id} has no WorkspaceAIConfig row; create one (enabled=True) first"
            )
        if not cfg.enabled and not force:
            raise CommandError(
                f"workspace {ws_id} has WorkspaceAIConfig.enabled=False; "
                "pass --force to override (do not on prod)"
            )

        excluded_projects = set(
            AIProjectSettings.objects.filter(exclude_from_ai=True).values_list(
                "project_id", flat=True
            )
        )

        # `n` is the global counter across all source types so the
        # rate limit applies cumulatively, not per-source-batch.
        n = 0
        per_source: dict[str, int] = {s: 0 for s in SOURCES}
        # Per-project breakdown for verbose mode + audit at the end.
        per_project: dict[str, dict[str, int]] = {}
        # Char-length accumulator for cost estimation in dry-run.
        total_chars = 0

        def schedule(
            source_type: str, project_id, source_id, char_len: int
        ) -> None:
            nonlocal n, total_chars
            total_chars += max(char_len, 0)
            key = str(project_id) if project_id else "<no-project>"
            bucket = per_project.setdefault(
                key, {s: 0 for s in SOURCES}
            )
            bucket[source_type] += 1
            if dry_run:
                per_source[source_type] += 1
                n += 1
                return
            reindex_source.apply_async(
                args=[
                    str(ws_id),
                    str(project_id) if project_id else None,
                    source_type,
                    str(source_id),
                ],
                countdown=n // rate,
            )
            per_source[source_type] += 1
            n += 1
            if n % 200 == 0:
                self.stdout.write(f"  ... enqueued {n} so far")

        # Tracks how many rows we skipped per excluded project. We
        # print this in verbose mode so the operator can sanity-check
        # the TZ 3.4 exclusion rather than trusting it silently.
        skipped_per_project: dict[str, int] = {}

        # ---- work_items ----
        if only_source in ("work_item", "all"):
            qs: QuerySet = Issue.objects.filter(
                workspace_id=ws_id, deleted_at__isnull=True, is_draft=False
            ).only("id", "project_id", "name", "description_stripped")
            for issue in self._batched(qs):
                if issue.project_id in excluded_projects:
                    key = str(issue.project_id)
                    skipped_per_project[key] = skipped_per_project.get(key, 0) + 1
                    continue
                # Combined char length matches what ai/chunking does:
                # title + description_stripped.
                clen = len(issue.name or "") + len(issue.description_stripped or "")
                schedule("work_item", issue.project_id, issue.id, clen)

        # ---- comments ----
        if only_source in ("comment", "all"):
            qs = IssueComment.objects.filter(
                workspace_id=ws_id, deleted_at__isnull=True
            ).only("id", "project_id", "comment_stripped")
            for comment in self._batched(qs):
                if comment.project_id in excluded_projects:
                    key = str(comment.project_id)
                    skipped_per_project[key] = skipped_per_project.get(key, 0) + 1
                    continue
                clen = len(getattr(comment, "comment_stripped", "") or "")
                schedule("comment", comment.project_id, comment.id, clen)

        # ---- pages ----
        if only_source in ("page", "all"):
            qs = Page.objects.filter(
                workspace_id=ws_id,
                deleted_at__isnull=True,
                archived_at__isnull=True,
            ).only("id", "name", "description_stripped")
            for page in self._batched(qs):
                # Page has no FK to project (SCHEMA.md). Multi-project
                # exclusion via ProjectPage is deferred to TZ 1.5
                # retrieval; backfill includes all non-archived pages.
                clen = (
                    len(getattr(page, "name", "") or "")
                    + len(getattr(page, "description_stripped", "") or "")
                )
                schedule("page", None, page.id, clen)

        verb = "Would enqueue" if dry_run else "Enqueued"
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb} {n} reindex tasks for workspace {ws_id} "
                f"(work_item={per_source['work_item']}, "
                f"comment={per_source['comment']}, "
                f"page={per_source['page']}, "
                f"rate={rate}/s)"
            )
        )

        # ---- TZ 6.6 cost estimate (dry-run only) -----------------
        if dry_run:
            est = self._estimate_cost(
                total_chars=total_chars,
                embed_model=cfg.embed_model or DEFAULT_EMBED_MODEL,
            )
            self.stdout.write(
                f"Estimated tokens: {est['tokens']:,} "
                f"(~{CHARS_PER_TOKEN:.0f} chars/token over {total_chars:,} chars)"
            )
            self.stdout.write(
                self.style.SUCCESS(
                    f"Estimated OpenAI embedding cost: ${est['usd']:.4f} "
                    f"(model={est['model']})"
                )
            )
            self.stdout.write(
                "Note: estimate is content-length-based. Actual billing "
                "is tokenizer-exact and recorded in AIUsageLog per chunk."
            )

        # ---- TZ 6.6 verification: which projects are EXCLUDED ----
        if verbose:
            if skipped_per_project:
                self.stdout.write("\nProjects excluded via AIProjectSettings.exclude_from_ai:")
                for prj, count in sorted(skipped_per_project.items()):
                    self.stdout.write(
                        f"  EXCLUDED  project={prj}  rows-skipped={count}"
                    )
            else:
                self.stdout.write(
                    "\nNo projects flagged exclude_from_ai — "
                    "confirm with the team that this is intended."
                )
            if per_project:
                self.stdout.write("\nPer-project breakdown of indexed rows:")
                for prj, bucket in sorted(per_project.items()):
                    self.stdout.write(
                        f"  project={prj}  "
                        f"work_item={bucket['work_item']}  "
                        f"comment={bucket['comment']}  "
                        f"page={bucket['page']}"
                    )

        # Hint: progress is visible via the index-status endpoint
        # (TZ 1.8) — count rows in ai_document_chunk vs work_items
        # in the workspace.
        if not dry_run:
            chunks = DocumentChunk.objects.filter(workspace_id=ws_id).count()
            self.stdout.write(f"current chunks for workspace: {chunks}")

    # ------------------------------------------------------------------

    @staticmethod
    def _estimate_cost(*, total_chars: int, embed_model: str) -> dict:
        """Approximate OpenAI embedding cost for ``total_chars``
        characters at ``embed_model``'s per-token rate.

        Pulled out for testability — the test pinned the math, so
        an accidental tweak to ``CHARS_PER_TOKEN`` shows up in red.
        """
        tokens = max(0, int(total_chars / CHARS_PER_TOKEN))
        usd: Decimal = embed_cost(embed_model, tokens)
        return {
            "tokens": tokens,
            "usd": float(usd),
            "model": embed_model,
        }

    @staticmethod
    def _batched(qs: QuerySet, chunk_size: int = 200) -> Iterable:
        """Wrap `.iterator(chunk_size=...)` so server-side cursor stays
        small for big workspaces."""
        return qs.iterator(chunk_size=chunk_size)
