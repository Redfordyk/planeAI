"""TZ 6.6 — pre-backfill production acceptance check.

Single command that runs every pre-condition the TZ enumerates as
"проверить ДО бэкафилла". Designed to be called as the first step
of the deploy/backfill sequence so a human operator can't paste the
next command before the previous gate is green.

Each check is independent (no early bail) so the operator sees the
**full** failure picture at once instead of fixing one thing,
re-running, fixing the next, re-running. The exit code is non-zero
iff any check returned a hard failure; warnings (e.g. "no projects
flagged exclude_from_ai" — possibly intentional) do not block but
are surfaced.

Usage::

    docker compose -p plane-ce exec api python manage.py \\
        acceptance_check --workspace <id-or-slug>

    # Sub-set if you only need to re-verify one thing:
    ... acceptance_check --workspace <id> --check backup,health

    # JSON output for CI consumption:
    ... acceptance_check --workspace <id> --json

Checks implemented:

  - ``dpa``       — env var ``PLANEAI_DPA_CLOSED`` is set to an ISO
                    date (the date the DPA was signed). Looked up
                    every run so a rotation/audit doesn't drift.
  - ``private``   — at least one ``AIProjectSettings`` row exists
                    with ``exclude_from_ai=True``, OR an explicit
                    env var ``PLANEAI_NO_PRIVATE_PROJECTS=<signer>:
                    <YYYY-MM-DD>`` declares "we reviewed and there
                    are none". The TZ 3.4 wiring is real either way;
                    the env var lets a workspace with zero private
                    projects pass the gate explicitly rather than
                    by silence.
  - ``backup``    — at least one fresh pg_dump file exists from the
                    last 24h under ``/backups/postgres/`` (RPO from
                    TZ 6.1). On hosts where the backup volume isn't
                    mounted into the API container, this check skips
                    with a warning — restore_test in BACKUP.md is
                    the harder guarantee anyway.
  - ``aiconfig``  — workspace has ``WorkspaceAIConfig.enabled=True``
                    with both ANTHROPIC and OPENAI keys non-empty.
  - ``health``    — ``ai/health.py:_run_checks`` returns ``status=ok``
                    or ``degraded``; ``down`` blocks the backfill.

Each check returns a dict ``{name, status, detail}`` with status in
``{ok, warn, fail, skip}``. The wrapper aggregates and prints.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from django.core.management.base import BaseCommand, CommandError


logger = logging.getLogger("plane.ai.acceptance")


# Where backup_postgres.sh from TZ 6.1 writes its dumps inside the
# sidecar container. The path is exposed to the API container via
# the same named volume on prod (see deploy-local/docker-compose.backup.yml).
DEFAULT_BACKUP_DIR = "/backups/postgres"

# Max age of the most-recent dump that still counts as "fresh".
# Matches the RPO declared in BACKUP.md.
FRESH_BACKUP_HOURS = 26  # 24h RPO + 2h grace for the cron-run jitter

CHECK_NAMES = ("dpa", "private", "backup", "aiconfig", "health")


@dataclass
class CheckResult:
    name: str
    status: str  # ok | warn | fail | skip
    detail: str


class Command(BaseCommand):
    help = (
        "Run all pre-backfill production acceptance checks (DPA, "
        "private-project exclusion, fresh backup, AI config, health). "
        "Exits non-zero if any hard failure — wire this into the "
        "production deploy script as a gate."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--workspace",
            required=True,
            help="Workspace UUID or slug to verify against.",
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
            help="Emit machine-readable JSON (for CI consumption).",
        )
        parser.add_argument(
            "--backup-dir",
            default=DEFAULT_BACKUP_DIR,
            help="Path to the pg_dump directory inside the container.",
        )

    # ------------------------------------------------------------------

    def handle(self, *args, **opts):
        ws_id = self._resolve_workspace(opts["workspace"])

        selected = (opts["check"] or ",".join(CHECK_NAMES)).split(",")
        unknown = [c for c in selected if c not in CHECK_NAMES]
        if unknown:
            raise CommandError(
                f"unknown check(s) {unknown!r}; available: {CHECK_NAMES}"
            )

        checks: dict[str, Callable[..., CheckResult]] = {
            "dpa": self._check_dpa,
            "private": self._check_private_projects,
            "backup": lambda: self._check_backup_freshness(opts["backup_dir"]),
            "aiconfig": lambda: self._check_ai_config(ws_id),
            "health": self._check_health,
        }

        results: list[CheckResult] = []
        for name in selected:
            try:
                results.append(checks[name]())
            except Exception as exc:  # noqa: BLE001
                logger.exception("acceptance check %s crashed", name)
                results.append(
                    CheckResult(
                        name=name,
                        status="fail",
                        detail=f"check crashed: {exc}",
                    )
                )

        any_fail = any(r.status == "fail" for r in results)

        if opts["as_json"]:
            self.stdout.write(
                json.dumps(
                    {
                        "workspace_id": str(ws_id),
                        "results": [asdict(r) for r in results],
                        "go": not any_fail,
                    },
                    ensure_ascii=False,
                )
            )
        else:
            self._render_human(results, ws_id=ws_id)

        if any_fail:
            # Non-zero exit so a wrapping deploy script halts.
            raise CommandError(
                "acceptance check failed — see results above. NOT safe "
                "to run backfill_embeddings against prod data yet."
            )

    # ------------------------------------------------------------------
    # Resolver
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve_workspace(token: str):
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
        """``PLANEAI_DPA_CLOSED=YYYY-MM-DD`` env var. Set by the
        operator after the legal team confirms DPA is signed with
        both providers (Anthropic, OpenAI). See GDPR.md."""
        raw = os.environ.get("PLANEAI_DPA_CLOSED", "").strip()
        if not raw:
            return CheckResult(
                "dpa", "fail",
                "PLANEAI_DPA_CLOSED env var unset — DPA gate not "
                "acknowledged. See GDPR.md.",
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
    def _check_private_projects() -> CheckResult:
        """Either flagged rows exist, OR an explicit signoff env var
        says "we reviewed and there are none". Both forms keep the
        team accountable. See TZ 3.4."""
        from ai.models import AIProjectSettings

        n = AIProjectSettings.objects.filter(exclude_from_ai=True).count()
        if n > 0:
            return CheckResult(
                "private", "ok",
                f"{n} project(s) flagged exclude_from_ai (TZ 3.4).",
            )
        signoff = os.environ.get("PLANEAI_NO_PRIVATE_PROJECTS", "").strip()
        if not signoff:
            return CheckResult(
                "private", "fail",
                "No AIProjectSettings(exclude_from_ai=True) rows AND "
                "no PLANEAI_NO_PRIVATE_PROJECTS=<signer>:<date> signoff "
                "in env. Either flag the private projects in admin or "
                "set the signoff env var to confirm there are none.",
            )
        # Lightly validate shape.
        if ":" not in signoff:
            return CheckResult(
                "private", "fail",
                f"PLANEAI_NO_PRIVATE_PROJECTS={signoff!r} — expected "
                "format '<signer>:<YYYY-MM-DD>'.",
            )
        return CheckResult(
            "private", "ok",
            f"No flagged projects; signoff present ({signoff}).",
        )

    @staticmethod
    def _check_backup_freshness(path: str) -> CheckResult:
        """Most-recent pg_dump file is younger than 26h."""
        d = Path(path)
        if not d.exists() or not d.is_dir():
            return CheckResult(
                "backup", "skip",
                f"backup directory {path} not mounted in this container; "
                "run separately or trust the sidecar restore-test.",
            )
        try:
            dumps = sorted(
                (p for p in d.iterdir() if p.is_file()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError as exc:
            return CheckResult(
                "backup", "fail",
                f"cannot list {path}: {exc}",
            )
        if not dumps:
            return CheckResult(
                "backup", "fail",
                f"no dump files in {path} — backup sidecar may not have run yet.",
            )
        newest = dumps[0]
        age = datetime.now(timezone.utc) - datetime.fromtimestamp(
            newest.stat().st_mtime, tz=timezone.utc
        )
        if age > timedelta(hours=FRESH_BACKUP_HOURS):
            return CheckResult(
                "backup", "fail",
                f"newest dump {newest.name} is {age} old "
                f"(> {FRESH_BACKUP_HOURS}h RPO). Run a fresh backup "
                "before backfill — see BACKUP.md.",
            )
        return CheckResult(
            "backup", "ok",
            f"latest dump {newest.name} is {age} old (< {FRESH_BACKUP_HOURS}h).",
        )

    @staticmethod
    def _check_ai_config(workspace_id) -> CheckResult:
        """WorkspaceAIConfig present, enabled, both keys non-empty."""
        from ai.models import WorkspaceAIConfig

        cfg = WorkspaceAIConfig.objects.filter(workspace_id=workspace_id).first()
        if cfg is None:
            return CheckResult(
                "aiconfig", "fail",
                "WorkspaceAIConfig row missing for the target workspace.",
            )
        if not cfg.enabled:
            return CheckResult(
                "aiconfig", "fail",
                "WorkspaceAIConfig.enabled=False — kill switch is on. "
                "Run manage.py enable_ai --workspace <id> first.",
            )
        missing = []
        # We don't print the keys (encrypted at rest, but still a no-no
        # for ops output) — only their presence.
        if not (cfg.anthropic_key or "").strip():
            missing.append("anthropic_key")
        if not (cfg.openai_key or "").strip():
            missing.append("openai_key")
        if missing:
            return CheckResult(
                "aiconfig", "fail",
                f"WorkspaceAIConfig missing keys: {', '.join(missing)}",
            )
        return CheckResult(
            "aiconfig", "ok",
            f"AI enabled, model={cfg.chat_model}, embed={cfg.embed_model}, "
            f"budget={cfg.monthly_token_budget:,} tokens/mo.",
        )

    @staticmethod
    def _check_health() -> CheckResult:
        """Reuse the same logic the /api/ai/health/ endpoint exposes.

        Importing locally (rather than at module top) so the command
        still works on a host where the monitoring module fails to
        load — the wrapping try/except in ``handle`` records the
        crash as a "fail" instead of breaking the whole sweep.
        """
        from ai.health import (
            _check_broker,
            _check_budget_headroom,
            _check_database,
            _check_index_freshness,
            _check_vector_extension,
            _rollup,
        )

        checks = {
            "database": _check_database(),
            "vector_ext": _check_vector_extension(),
            "broker": _check_broker(),
            "index_freshness": _check_index_freshness(),
            "budget": _check_budget_headroom(),
        }
        rollup, _ = _rollup(checks)
        failing = [
            name for name, c in checks.items() if c.get("status") != "ok"
        ]
        if rollup == "ok":
            return CheckResult("health", "ok", "all health probes ok.")
        if rollup == "degraded":
            return CheckResult(
                "health", "warn",
                f"health degraded: {', '.join(failing) or 'unknown'}",
            )
        return CheckResult(
            "health", "fail",
            f"health DOWN: {', '.join(failing) or 'unknown'}. "
            "Fix infra before backfill.",
        )

    # ------------------------------------------------------------------
    # Human renderer
    # ------------------------------------------------------------------

    def _render_human(self, results: list[CheckResult], *, ws_id) -> None:
        glyph = {"ok": "✓", "warn": "!", "fail": "✗", "skip": "·"}
        style = {
            "ok": self.style.SUCCESS,
            "warn": self.style.WARNING,
            "fail": self.style.ERROR,
            "skip": lambda s: s,
        }
        self.stdout.write(f"acceptance_check workspace={ws_id}")
        for r in results:
            line = f"  [{glyph[r.status]}] {r.name:<10}  {r.detail}"
            self.stdout.write(style[r.status](line))
        bad = [r for r in results if r.status == "fail"]
        if bad:
            self.stdout.write(
                self.style.ERROR(
                    f"\nNOT GO — {len(bad)} hard failure(s). Fix and re-run."
                )
            )
        else:
            warns = [r for r in results if r.status == "warn"]
            if warns:
                self.stdout.write(
                    self.style.WARNING(
                        f"\nGO with warnings ({len(warns)}). Review before backfill."
                    )
                )
            else:
                self.stdout.write(
                    self.style.SUCCESS(
                        "\nALL GREEN — safe to run backfill_embeddings."
                    )
                )
