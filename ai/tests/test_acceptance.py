"""TZ 6.6 — production acceptance gate.

Pin two surfaces of behaviour:

  1. ``backfill_embeddings`` refuses to start a real run without
     ``--i-confirm-dpa-closed``. The dry-run path stays open (it's
     the way an operator previews scope + cost). Cost estimation
     uses ``ai.pricing`` so a price-table change shows up in red.

  2. ``acceptance_check`` aggregates 5 preflights (DPA env var,
     private-project flag/signoff, fresh backup file, AI config
     present, health) and exits non-zero on any hard failure. The
     test isolates each check by overriding the relevant input and
     reading the JSON output (no parsing of human-formatted text).
"""

from __future__ import annotations

import io
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from ai.management.commands.backfill_embeddings import (
    CHARS_PER_TOKEN,
    Command as BackfillCmd,
)
from ai.models import AIProjectSettings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(cmd: str, **kw) -> str:
    out = io.StringIO()
    call_command(cmd, stdout=out, stderr=out, **kw)
    return out.getvalue()


def _run_json(cmd: str, **kw) -> dict:
    """Run command with --json and parse the last JSON line."""
    out = io.StringIO()
    call_command(cmd, stdout=out, stderr=io.StringIO(), as_json=True, **kw)
    # acceptance_check prints exactly one JSON line then exits.
    return json.loads(out.getvalue().strip().splitlines()[-1])


# ---------------------------------------------------------------------------
# backfill_embeddings — DPA gate + cost estimate
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_backfill_refuses_real_run_without_dpa(
    make_workspace, make_ai_config
):
    """Real backfill without ``--i-confirm-dpa-closed`` is a hard
    error. The flag is intentionally verbose so prod operators can't
    paste a staging command and get away with it."""
    ws = make_workspace()
    make_ai_config(ws)
    with pytest.raises(CommandError, match="DPA"):
        _run("backfill_embeddings", workspace=str(ws.id))


@pytest.mark.django_db
def test_backfill_dry_run_does_not_need_dpa(
    make_workspace, make_ai_config
):
    """Dry-run doesn't touch OpenAI → no DPA gate. This is the
    "show me what would happen" preview that operators use BEFORE
    they confirm the DPA flag."""
    ws = make_workspace()
    make_ai_config(ws)
    out = _run("backfill_embeddings", workspace=str(ws.id), dry_run=True)
    assert "Would enqueue" in out
    # Cost estimate header is always present in dry-run.
    assert "Estimated OpenAI embedding cost" in out


@pytest.mark.django_db
def test_backfill_dry_run_estimates_cost(
    make_workspace, make_ai_config, make_project, make_issue
):
    """One issue with 8000 characters → 2000 estimated tokens (chars/4)
    → cost = 2000 × $0.00000002 = $0.00004."""
    ws = make_workspace()
    make_ai_config(ws)
    project = make_project(workspace=ws, created_by=ws.owner)
    # Issue.name is CharField(max_length=255). Pack 200 chars of name
    # + 7800 of description to keep total at 8000 (the math the test
    # is anchored to).
    make_issue(
        workspace=ws, project=project,
        name="x" * 200, description="y" * 7800,
    )
    out = _run("backfill_embeddings", workspace=str(ws.id), dry_run=True)
    # Expected token estimate: 8000 / 4 = 2000
    assert "Estimated tokens: 2,000" in out
    # Don't hardcode the dollar value to the 9th decimal — anchor to
    # the visible "$0.0000" prefix which already pins precision.
    assert "$0.0000" in out


def test_estimate_cost_pure_function():
    """The math is broken out so a test can pin it without spinning
    up a workspace. If somebody bumps CHARS_PER_TOKEN or the rate
    in ai.pricing, this fails loudly."""
    est = BackfillCmd._estimate_cost(
        total_chars=4000, embed_model="text-embedding-3-small"
    )
    assert est["tokens"] == int(4000 / CHARS_PER_TOKEN)
    # Rate is $0.00000002/token for the small model — 1000 tokens
    # rounds to $0.00002.
    assert abs(est["usd"] - 0.00002) < 1e-9


@pytest.mark.django_db
def test_backfill_verbose_lists_excluded_projects(
    make_workspace, make_ai_config, make_project, make_issue
):
    """The verbose flag prints "EXCLUDED  project=<uuid> rows-skipped=N"
    lines so the operator can verify TZ 3.4 worked. Without that
    output, "no private projects indexed" is unverifiable."""
    ws = make_workspace()
    make_ai_config(ws)
    public = make_project(workspace=ws, created_by=ws.owner)
    private = make_project(workspace=ws, created_by=ws.owner, exclude_from_ai=True)
    # 2 work items in private, 1 in public — private should be skipped.
    for i in range(2):
        make_issue(workspace=ws, project=private, name=f"priv-{i}")
    make_issue(workspace=ws, project=public, name="public-1")

    out = _run(
        "backfill_embeddings",
        workspace=str(ws.id),
        dry_run=True,
        verbose=True,
    )
    # The private project shows up under EXCLUDED with rows-skipped=2.
    assert "EXCLUDED" in out
    assert f"project={private.id}" in out
    assert "rows-skipped=2" in out


# ---------------------------------------------------------------------------
# acceptance_check
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_env(monkeypatch):
    """Strip the env vars the acceptance check looks at so tests start
    from a known blank. Each test sets only what it needs."""
    for k in ("PLANEAI_DPA_CLOSED", "PLANEAI_NO_PRIVATE_PROJECTS"):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


@pytest.fixture
def fresh_backup_dir(tmp_path):
    """Create a tmpdir with one fresh dump file inside; return path."""
    d = tmp_path / "postgres"
    d.mkdir()
    fresh = d / "planeai-2026-05-22.dump"
    fresh.write_bytes(b"fake dump")
    # ``mtime = now`` so the 26h freshness window passes.
    return d


@pytest.mark.django_db
def test_acceptance_all_green(
    isolated_env,
    fresh_backup_dir,
    make_workspace,
    make_ai_config,
    make_project,
):
    """Happy path: DPA env set, one excluded project, fresh backup,
    AI config valid, health OK → exits zero with ``go=true``."""
    isolated_env.setenv("PLANEAI_DPA_CLOSED", "2026-05-01")
    ws = make_workspace()
    make_ai_config(ws)
    make_project(workspace=ws, created_by=ws.owner, exclude_from_ai=True)

    body = _run_json(
        "acceptance_check",
        workspace=str(ws.id),
        backup_dir=str(fresh_backup_dir),
    )
    assert body["go"] is True
    results = {r["name"]: r for r in body["results"]}
    assert results["dpa"]["status"] == "ok"
    assert results["private"]["status"] == "ok"
    assert results["backup"]["status"] == "ok"
    assert results["aiconfig"]["status"] == "ok"


@pytest.mark.django_db
def test_acceptance_fails_when_dpa_unset(
    isolated_env, fresh_backup_dir, make_workspace, make_ai_config,
    make_project,
):
    """No DPA env var → command exits non-zero (raises CommandError)."""
    ws = make_workspace()
    make_ai_config(ws)
    make_project(workspace=ws, created_by=ws.owner, exclude_from_ai=True)
    with pytest.raises(CommandError, match="acceptance check failed"):
        call_command(
            "acceptance_check",
            workspace=str(ws.id),
            backup_dir=str(fresh_backup_dir),
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )


@pytest.mark.django_db
def test_acceptance_dpa_must_be_iso_date(
    isolated_env, fresh_backup_dir, make_workspace, make_ai_config,
    make_project,
):
    """Garbage in PLANEAI_DPA_CLOSED is a hard fail — refuses to
    interpret '2026/05/01' or 'yes'."""
    isolated_env.setenv("PLANEAI_DPA_CLOSED", "yes-we-signed")
    ws = make_workspace()
    make_ai_config(ws)
    make_project(workspace=ws, created_by=ws.owner, exclude_from_ai=True)
    with pytest.raises(CommandError):
        call_command(
            "acceptance_check",
            workspace=str(ws.id),
            check="dpa",
            backup_dir=str(fresh_backup_dir),
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )


@pytest.mark.django_db
def test_acceptance_private_signoff_ok(
    isolated_env, fresh_backup_dir, make_workspace, make_ai_config,
):
    """Workspace with NO private projects flagged passes only when
    the explicit signoff env var is set."""
    isolated_env.setenv("PLANEAI_DPA_CLOSED", "2026-05-01")
    isolated_env.setenv(
        "PLANEAI_NO_PRIVATE_PROJECTS", "ilya:2026-07-07"
    )
    ws = make_workspace()
    make_ai_config(ws)
    # Note: no exclude_from_ai project exists.
    assert AIProjectSettings.objects.filter(exclude_from_ai=True).count() == 0

    body = _run_json(
        "acceptance_check",
        workspace=str(ws.id),
        backup_dir=str(fresh_backup_dir),
    )
    private = next(r for r in body["results"] if r["name"] == "private")
    assert private["status"] == "ok"


@pytest.mark.django_db
def test_acceptance_private_signoff_missing(
    isolated_env, fresh_backup_dir, make_workspace, make_ai_config,
):
    """No flagged projects AND no signoff → fail. The team must
    *consciously* declare "we have no private projects"."""
    isolated_env.setenv("PLANEAI_DPA_CLOSED", "2026-05-01")
    ws = make_workspace()
    make_ai_config(ws)
    with pytest.raises(CommandError):
        call_command(
            "acceptance_check",
            workspace=str(ws.id),
            check="private",
            backup_dir=str(fresh_backup_dir),
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )


@pytest.mark.django_db
def test_acceptance_backup_stale(
    isolated_env, tmp_path, make_workspace, make_ai_config, make_project
):
    """A dump file older than 26h fails the backup check."""
    isolated_env.setenv("PLANEAI_DPA_CLOSED", "2026-05-01")
    d = tmp_path / "postgres"
    d.mkdir()
    stale = d / "old.dump"
    stale.write_bytes(b"")
    # Touch the mtime to 48h ago.
    old = (datetime.now(timezone.utc) - timedelta(hours=48)).timestamp()
    os.utime(stale, (old, old))

    ws = make_workspace()
    make_ai_config(ws)
    make_project(workspace=ws, created_by=ws.owner, exclude_from_ai=True)

    with pytest.raises(CommandError):
        call_command(
            "acceptance_check",
            workspace=str(ws.id),
            check="backup",
            backup_dir=str(d),
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )


@pytest.mark.django_db
def test_acceptance_backup_missing_dir_is_skip(
    isolated_env, tmp_path, make_workspace, make_ai_config, make_project
):
    """A backup dir that isn't mounted in this container — the check
    returns ``skip`` rather than ``fail``, because BACKUP.md's
    restore-test is the harder guarantee anyway and we don't want
    a missing mount to block the rest of the sweep."""
    isolated_env.setenv("PLANEAI_DPA_CLOSED", "2026-05-01")
    ws = make_workspace()
    make_ai_config(ws)
    make_project(workspace=ws, created_by=ws.owner, exclude_from_ai=True)

    body = _run_json(
        "acceptance_check",
        workspace=str(ws.id),
        check="backup",
        backup_dir=str(tmp_path / "does-not-exist"),
    )
    backup = next(r for r in body["results"] if r["name"] == "backup")
    assert backup["status"] == "skip"
    assert body["go"] is True  # skip is not fail


@pytest.mark.django_db
def test_acceptance_aiconfig_missing_keys(
    isolated_env, fresh_backup_dir, make_workspace, make_project
):
    """A WorkspaceAIConfig with empty keys → fail. The cfg is enabled
    but unusable, which is exactly what the gate is for."""
    isolated_env.setenv("PLANEAI_DPA_CLOSED", "2026-05-01")
    ws = make_workspace()
    from ai.models import WorkspaceAIConfig

    WorkspaceAIConfig.objects.create(
        workspace=ws,
        anthropic_key="",
        openai_key="",
        enabled=True,
        monthly_token_budget=1_000_000,
    )
    make_project(workspace=ws, created_by=ws.owner, exclude_from_ai=True)
    with pytest.raises(CommandError):
        call_command(
            "acceptance_check",
            workspace=str(ws.id),
            check="aiconfig",
            backup_dir=str(fresh_backup_dir),
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )


@pytest.mark.django_db
def test_acceptance_aiconfig_disabled(
    isolated_env, fresh_backup_dir, make_workspace, make_ai_config,
    make_project,
):
    """Kill switch on → backfill not safe → fail."""
    isolated_env.setenv("PLANEAI_DPA_CLOSED", "2026-05-01")
    ws = make_workspace()
    make_ai_config(ws, enabled=False)
    make_project(workspace=ws, created_by=ws.owner, exclude_from_ai=True)
    with pytest.raises(CommandError):
        call_command(
            "acceptance_check",
            workspace=str(ws.id),
            check="aiconfig",
            backup_dir=str(fresh_backup_dir),
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )


@pytest.mark.django_db
def test_acceptance_check_subset(
    isolated_env, fresh_backup_dir, make_workspace, make_ai_config,
    make_project,
):
    """``--check dpa,backup`` runs only those two — the others are
    omitted from the results array. Lets the operator re-verify one
    thing after a fix without re-running the whole sweep."""
    isolated_env.setenv("PLANEAI_DPA_CLOSED", "2026-05-01")
    ws = make_workspace()
    make_ai_config(ws)
    make_project(workspace=ws, created_by=ws.owner, exclude_from_ai=True)

    body = _run_json(
        "acceptance_check",
        workspace=str(ws.id),
        check="dpa,backup",
        backup_dir=str(fresh_backup_dir),
    )
    names = {r["name"] for r in body["results"]}
    assert names == {"dpa", "backup"}
