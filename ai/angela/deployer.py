"""Deployment strategies for Angela — three modes, three buttons.

All three operate ONLY on the sandbox target, so none of them can reach
a real production system. "prod" here means the sandbox's own prod-like
environment (e.g. a second compose stack), declared per-target as
``prod_deploy_cmd`` / ``prod_url``.

    MODE_STAGING_GATE     deploy to staging automatically, then PAUSE in
                          ``awaiting_approval`` — a human presses the
                          "Approve prod" button to run the prod deploy.
                          (Default; matches CLAUDE.md "ACL in code, the
                          model proposes, a human authorises the risky
                          step".)
    MODE_AUTONOMOUS_PROD  staging then prod, no human in the loop.
    MODE_MANUAL           no deploy at all here; the artifact (branch +
                          green tests) is left for a human to ship via
                          the "Deploy now" button.

The pipeline calls :func:`run_deploy` after tests pass. The approve and
manual buttons call :func:`deploy_prod` directly via the API.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ai.models import AngelaRun, AngelaStep

from .base import log_step, set_status
from .sandbox import Sandbox


logger = logging.getLogger("plane.ai.angela.deployer")


@dataclass
class DeployOutcome:
    ok: bool
    target_env: str          # "staging" | "prod" | ""
    url: str
    detail: str


def _do_deploy(sandbox: Sandbox, *, env: str) -> DeployOutcome:
    """Run the configured deploy command for ``env`` ('staging'|'prod')."""
    target = sandbox.target
    if env == "prod":
        cmd, url = target.prod_deploy_cmd, target.prod_url
    else:
        cmd, url = target.staging_deploy_cmd, target.staging_url

    if not cmd:
        # No command configured → treat as a dry-run "deployment" so the
        # demo flow completes end-to-end without real infra.
        return DeployOutcome(
            ok=True, target_env=env, url=url,
            detail=f"(dry-run) no {env}_deploy_cmd configured; artifact ready at branch.",
        )
    res = sandbox.run_shell(cmd)
    return DeployOutcome(
        ok=res.ok, target_env=env, url=url if res.ok else "", detail=res.tail(2000)
    )


def deploy_staging(run: AngelaRun, sandbox: Sandbox) -> DeployOutcome:
    log_step(run, phase=AngelaStep.PHASE_DEPLOY, status=AngelaStep.STATUS_STARTED,
             title="deploy → staging")
    out = _do_deploy(sandbox, env="staging")
    log_step(
        run, phase=AngelaStep.PHASE_DEPLOY,
        status=AngelaStep.STATUS_OK if out.ok else AngelaStep.STATUS_FAILED,
        title=f"staging {'ok' if out.ok else 'failed'}", detail=out.detail,
    )
    return out


def deploy_prod(run: AngelaRun, sandbox: Sandbox) -> DeployOutcome:
    log_step(run, phase=AngelaStep.PHASE_DEPLOY, status=AngelaStep.STATUS_STARTED,
             title="deploy → prod (sandbox)")
    out = _do_deploy(sandbox, env="prod")
    log_step(
        run, phase=AngelaStep.PHASE_DEPLOY,
        status=AngelaStep.STATUS_OK if out.ok else AngelaStep.STATUS_FAILED,
        title=f"prod {'ok' if out.ok else 'failed'}", detail=out.detail,
    )
    return out


def run_deploy(run: AngelaRun, sandbox: Sandbox) -> None:
    """Dispatch on ``run.deploy_mode`` after tests are green.

    Advances the run to a terminal state (succeeded/failed) or to
    ``awaiting_approval`` for the staging-gate mode.
    """
    mode = run.deploy_mode

    if mode == AngelaRun.MODE_MANUAL:
        log_step(run, phase=AngelaStep.PHASE_DEPLOY, status=AngelaStep.STATUS_SKIPPED,
                 title="manual mode — deploy left to a human",
                 detail="Press “Deploy now” to ship the green artifact.")
        set_status(run, AngelaRun.STATUS_SUCCEEDED, deploy_target="")
        return

    # both gate + autonomous deploy to staging first
    staging = deploy_staging(run, sandbox)
    if not staging.ok:
        set_status(run, AngelaRun.STATUS_FAILED, deploy_target="staging",
                   error="staging deploy failed:\n" + staging.detail[:2000])
        return

    if mode == AngelaRun.MODE_STAGING_GATE:
        set_status(
            run, AngelaRun.STATUS_AWAITING_APPROVAL,
            deploy_target="staging", deploy_url=staging.url,
        )
        log_step(run, phase=AngelaStep.PHASE_DEPLOY, status=AngelaStep.STATUS_OK,
                 title="awaiting human approval for prod",
                 detail="Staging is live. Approve to deploy to prod.")
        return

    # MODE_AUTONOMOUS_PROD → straight to prod
    prod = deploy_prod(run, sandbox)
    if not prod.ok:
        set_status(run, AngelaRun.STATUS_FAILED, deploy_target="prod",
                   deploy_url=staging.url, error="prod deploy failed:\n" + prod.detail[:2000])
        return
    set_status(run, AngelaRun.STATUS_SUCCEEDED, deploy_target="prod", deploy_url=prod.url)
