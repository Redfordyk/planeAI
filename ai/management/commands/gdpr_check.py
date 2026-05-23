"""TZ 6.7 — GDPR release-gate verification.

Standalone sibling of ``acceptance_check`` (TZ 6.6). They overlap
intentionally on the DPA env-var check; the rest is GDPR-specific.

Each check is independent (no early bail) so the responsible person
(usually PM) sees the full picture in one pass. Non-zero exit means
"do NOT sign GDPR-RELEASE.md yet".

Checks implemented:

  - ``dpa``                ``PLANEAI_DPA_CLOSED`` env var holds an
                           ISO date. Mirror of acceptance_check.dpa.
  - ``encryption_key``     ``FIELD_ENCRYPTION_KEY`` env var is set
                           and non-default. Without it,
                           ``WorkspaceAIConfig.anthropic_key`` /
                           ``openai_key`` can't be decrypted, and the
                           "data at rest" GDPR principle is broken.
                           CRITICAL — losing this key means losing
                           every workspace's AI config permanently.
  - ``private_clean``      For every project with
                           ``AIProjectSettings.exclude_from_ai=True``,
                           there must be ZERO ``DocumentChunk`` rows.
                           Closes the right-to-erasure loop after
                           retroactive flagging.
  - ``deleted_clean``      No DocumentChunk row points at a soft-
                           deleted source (Issue / IssueComment /
                           Page with ``deleted_at IS NOT NULL``).
                           Closes "right to erasure on data deletion"
                           in the steady state.
  - ``feature_complete``   Every AIUsageLog row has ``feature`` set
                           to a known constant (not NULL, not empty).
                           Without correct feature tagging the
                           BUDGET dashboard lies (TZ 6.3), which
                           breaks the data-minimisation reporting
                           promise to the team.

Usage::

    python manage.py gdpr_check                   # all checks, all workspaces
    python manage.py gdpr_check --workspace <id>  # scope to one workspace
    python manage.py gdpr_check --json            # machine-readable
    python manage.py gdpr_check --check private_clean,deleted_clean

The output is the kind of paragraph the PM pastes into the GDPR-
RELEASE.md "evidence" column. JSON mode is wired for CI; the human
mode prints check-by-check status with green/yellow/red glyphs.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Callable

from django.core.management.base import BaseCommand, CommandError


logger = logging.getLogger("plane.ai.gdpr")


CHECK_NAMES = (
    "dpa",
    "encryption_key",
    "private_clean",
    "deleted_clean",
    "feature_complete",
)


@dataclass
class CheckResult:
    name: str
    status: str  # ok | warn | fail
    detail: str
    # Optional structured payload — JSON mode surfaces the counts so
    # the operator can paste them into the release doc without
    # re-running queries.
    metrics: dict | None = None


class Command(BaseCommand):
    help = (
        "GDPR release-gate verification (TZ 6.7). Aggregates DPA, "
        "encryption key presence, private-project cleanliness, "
        "deleted-source cleanliness, and AIUsageLog feature tagging. "
        "Exits non-zero on any hard failure — wire into the release "
        "pipeline as a blocking gate."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--workspace",
            help=(
                "Optional workspace scope (UUID or slug). Without it, "
                "checks span the whole installation."
            ),
        )
        parser.add_argument(
            "--check",
            help=(
                "Comma-separated subset of checks to run. Default: all. "
                f"Available: {','.join(CHECK_NAMES)}."
            ),
        )
        parser.add_argument(
            "--json",
            dest="as_json",
            action="store_true",
            help="Emit machine-readable JSON.",
        )

    # ------------------------------------------------------------------

    def handle(self, *args, **opts):
        ws_id = self._resolve_workspace(opts.get("workspace"))

        selected = (opts["check"] or ",".join(CHECK_NAMES)).split(",")
        unknown = [c for c in selected if c not in CHECK_NAMES]
        if unknown:
            raise CommandError(
                f"unknown check(s) {unknown!r}; available: {CHECK_NAMES}"
            )

        checks: dict[str, Callable[..., CheckResult]] = {
            "dpa": self._check_dpa,
            "encryption_key": self._check_encryption_key,
            "private_clean": lambda: self._check_private_clean(ws_id),
            "deleted_clean": lambda: self._check_deleted_clean(ws_id),
            "feature_complete": lambda: self._check_feature_complete(ws_id),
        }

        results: list[CheckResult] = []
        for name in selected:
            try:
                results.append(checks[name]())
            except Exception as exc:  # noqa: BLE001 — crash logged
                logger.exception("gdpr_check %s crashed", name)
                results.append(
                    CheckResult(
                        name=name, status="fail",
                        detail=f"check crashed: {exc}",
                    )
                )

        any_fail = any(r.status == "fail" for r in results)

        if opts["as_json"]:
            self.stdout.write(
                json.dumps(
                    {
                        "workspace_id": str(ws_id) if ws_id else None,
                        "results": [asdict(r) for r in results],
                        "go": not any_fail,
                    },
                    ensure_ascii=False,
                )
            )
        else:
            self._render_human(results, ws_id=ws_id)

        if any_fail:
            raise CommandError(
                "GDPR check failed — see results above. NOT safe to "
                "sign GDPR-RELEASE.md yet."
            )

    # ------------------------------------------------------------------
    # Resolver
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
    # Individual checks
    # ------------------------------------------------------------------

    @staticmethod
    def _check_dpa() -> CheckResult:
        """Same gate as acceptance_check.dpa — reproduced here so the
        GDPR pass is self-contained (the PM might run gdpr_check on
        its own before signing the release doc)."""
        raw = os.environ.get("PLANEAI_DPA_CLOSED", "").strip()
        if not raw:
            return CheckResult(
                "dpa", "fail",
                "PLANEAI_DPA_CLOSED env var unset. DPA gate not "
                "acknowledged — see GDPR.md.",
            )
        try:
            signed = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            return CheckResult(
                "dpa", "fail",
                f"PLANEAI_DPA_CLOSED={raw!r} is not ISO date (YYYY-MM-DD).",
            )
        return CheckResult(
            "dpa", "ok",
            f"DPA acknowledged closed on {signed.isoformat()}.",
        )

    @staticmethod
    def _check_encryption_key() -> CheckResult:
        """FIELD_ENCRYPTION_KEY must be set in the runtime env. This
        is what encrypts the API keys at rest in WorkspaceAIConfig.

        We deliberately do NOT print the key value — only its
        presence + a length sanity check. Logging the key would
        defeat the entire encryption-at-rest setup.
        """
        key = os.environ.get("FIELD_ENCRYPTION_KEY", "").strip()
        if not key:
            return CheckResult(
                "encryption_key", "fail",
                "FIELD_ENCRYPTION_KEY unset. Encryption-at-rest for "
                "WorkspaceAIConfig keys is broken. Run "
                "scripts/gen_encryption_key.py and put the result in "
                "deploy-local/.env (and your secrets backup).",
            )
        if key in ("CHANGE_ME", "changeme", "fake", "test"):
            return CheckResult(
                "encryption_key", "fail",
                f"FIELD_ENCRYPTION_KEY={key!r} is a placeholder — "
                "rotate before any prod traffic.",
            )
        if len(key) < 32:
            return CheckResult(
                "encryption_key", "fail",
                f"FIELD_ENCRYPTION_KEY length={len(key)} is too short "
                "for Fernet (expected 44 url-safe base64 chars). "
                "Re-generate via scripts/gen_encryption_key.py.",
            )
        return CheckResult(
            "encryption_key", "ok",
            f"FIELD_ENCRYPTION_KEY present (length={len(key)}).",
        )

    @staticmethod
    def _check_private_clean(workspace_id) -> CheckResult:
        """For every project with exclude_from_ai=True, DocumentChunk
        must be empty. The signal added in TZ 6.7 keeps this true
        going forward; the check confirms historical state too."""
        from ai.models import AIProjectSettings, DocumentChunk

        excluded = AIProjectSettings.objects.filter(exclude_from_ai=True)
        if workspace_id is not None:
            # AIProjectSettings has no workspace FK directly — go
            # via the project. We resolve via the Project model.
            from django.apps import apps as django_apps
            Project = django_apps.get_model("db", "Project")
            excluded_project_ids = list(
                Project.objects.filter(
                    workspace_id=workspace_id,
                    id__in=excluded.values_list("project_id", flat=True),
                ).values_list("id", flat=True)
            )
        else:
            excluded_project_ids = list(
                excluded.values_list("project_id", flat=True)
            )

        if not excluded_project_ids:
            return CheckResult(
                "private_clean", "ok",
                "no projects flagged exclude_from_ai (nothing to check).",
                metrics={"private_projects": 0, "leaked_chunks": 0},
            )

        leaked = (
            DocumentChunk.objects.filter(project_id__in=excluded_project_ids)
            .count()
        )
        if leaked > 0:
            # Identify which projects leaked so the operator can run
            # the purge command per-project rather than a global wipe.
            offenders = (
                DocumentChunk.objects.filter(
                    project_id__in=excluded_project_ids
                )
                .values_list("project_id", flat=True)
                .distinct()
            )
            offender_ids = [str(p) for p in offenders]
            return CheckResult(
                "private_clean", "fail",
                f"{leaked} DocumentChunk rows found in "
                f"{len(offender_ids)} excluded project(s): "
                f"{', '.join(offender_ids)}. Run "
                "manage.py shell and `DocumentChunk.objects."
                "filter(project_id__in=[...]).delete()` to purge, "
                "or re-save the AIProjectSettings row to fire the "
                "TZ 6.7 cleanup signal.",
                metrics={
                    "private_projects": len(excluded_project_ids),
                    "leaked_chunks": leaked,
                    "offender_project_ids": offender_ids,
                },
            )
        return CheckResult(
            "private_clean", "ok",
            f"all {len(excluded_project_ids)} excluded project(s) clean.",
            metrics={
                "private_projects": len(excluded_project_ids),
                "leaked_chunks": 0,
            },
        )

    @staticmethod
    def _check_deleted_clean(workspace_id) -> CheckResult:
        """No chunk should reference a soft-deleted source. We check
        per source_type — Plane's soft-delete writes deleted_at to
        Issue/IssueComment/Page, but DocumentChunk has no FK back
        (only source_type + source_id) so the join is by id."""
        from django.apps import apps as django_apps

        from ai.models import DocumentChunk

        offenders: dict[str, int] = {}

        def _scan(source_type: str, model_name: str) -> None:
            Model = django_apps.get_model("db", model_name)
            base = DocumentChunk.objects.filter(source_type=source_type)
            if workspace_id is not None:
                base = base.filter(workspace_id=workspace_id)
            chunk_source_ids = base.values_list(
                "source_id", flat=True
            ).distinct()
            if not chunk_source_ids:
                return
            # Source rows that are soft-deleted but still have chunks.
            deleted_in_db = set(
                Model.objects.filter(
                    id__in=list(chunk_source_ids),
                    deleted_at__isnull=False,
                ).values_list("id", flat=True)
            )
            if deleted_in_db:
                offenders[source_type] = len(deleted_in_db)

        _scan(DocumentChunk.SOURCE_WORK_ITEM, "Issue")
        _scan(DocumentChunk.SOURCE_COMMENT, "IssueComment")
        _scan(DocumentChunk.SOURCE_PAGE, "Page")

        if not offenders:
            return CheckResult(
                "deleted_clean", "ok",
                "no chunks reference soft-deleted sources.",
                metrics={"stale_by_type": {}},
            )
        return CheckResult(
            "deleted_clean", "fail",
            f"chunks pointing at soft-deleted sources: {offenders}. "
            "Either the delete_chunks signal didn't fire (Celery "
            "broker dead?) or somebody hand-edited the DB. Use "
            "manage.py shell to purge.",
            metrics={"stale_by_type": offenders},
        )

    @staticmethod
    def _check_feature_complete(workspace_id) -> CheckResult:
        """Every AIUsageLog row must have a known feature tag. Without
        it, the BUDGET dashboard misrepresents spend per feature
        (TZ 6.3) and the team can't honour the minimisation promise
        ("we tell the team what gets sent for which use case")."""
        from ai.models import AIUsageLog

        valid = {f[0] for f in AIUsageLog.FEATURE_CHOICES}
        base = AIUsageLog.objects.all()
        if workspace_id is not None:
            base = base.filter(workspace_id=workspace_id)
        invalid = base.exclude(feature__in=valid).count()
        if invalid > 0:
            return CheckResult(
                "feature_complete", "fail",
                f"{invalid} AIUsageLog row(s) have unknown/empty "
                "feature. Newer features must call record_usage() "
                "with one of AIUsageLog.FEATURE_*. See BUDGET.md "
                "'Audit feature tagging'.",
                metrics={"invalid_rows": invalid},
            )
        return CheckResult(
            "feature_complete", "ok",
            "all AIUsageLog rows have a valid feature tag.",
            metrics={"invalid_rows": 0},
        )

    # ------------------------------------------------------------------
    # Human renderer
    # ------------------------------------------------------------------

    def _render_human(self, results: list[CheckResult], *, ws_id) -> None:
        glyph = {"ok": "✓", "warn": "!", "fail": "✗"}
        style = {
            "ok": self.style.SUCCESS,
            "warn": self.style.WARNING,
            "fail": self.style.ERROR,
        }
        scope = f"workspace={ws_id}" if ws_id else "scope=installation"
        self.stdout.write(f"gdpr_check {scope}")
        for r in results:
            line = f"  [{glyph.get(r.status, '?')}] {r.name:<18}  {r.detail}"
            renderer = style.get(r.status, lambda s: s)
            self.stdout.write(renderer(line))
        bad = [r for r in results if r.status == "fail"]
        if bad:
            self.stdout.write(
                self.style.ERROR(
                    f"\nNOT GO — {len(bad)} GDPR failure(s). DO NOT "
                    "sign GDPR-RELEASE.md."
                )
            )
        else:
            self.stdout.write(
                self.style.SUCCESS(
                    "\nALL GREEN — safe to sign GDPR-RELEASE.md."
                )
            )
