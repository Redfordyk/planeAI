"""Celery entry points for Angela.

The API view enqueues ``angela_run`` so the bounded code→review→test→
deploy loop never executes in the request thread (it shells out to git,
test runners, and deploy scripts that can take minutes).
"""

from __future__ import annotations

import logging

from celery import shared_task

from . import pipeline


logger = logging.getLogger("plane.ai.angela.tasks")


@shared_task(name="ai.angela_run", bind=True, max_retries=0, acks_late=False)
def angela_run(self, run_id: str, run_docs: bool = True):
    """Execute the full Angela pipeline for a queued run."""
    logger.info("angela_run start run=%s", run_id)
    pipeline.run_pipeline(str(run_id), run_docs=bool(run_docs))


@shared_task(name="ai.angela_approve_prod", bind=True, max_retries=0)
def angela_approve_prod(self, run_id: str, approver_id: str):
    """Run the gated prod deploy after a human approves (staging_gate mode)."""
    pipeline.approve_prod_deploy(str(run_id), approver_id)


@shared_task(name="ai.angela_manual_deploy", bind=True, max_retries=0)
def angela_manual_deploy(self, run_id: str, deployer_id: str):
    """Ship a green artifact from MODE_MANUAL via the 'Deploy now' button."""
    pipeline.manual_deploy(str(run_id), deployer_id)
