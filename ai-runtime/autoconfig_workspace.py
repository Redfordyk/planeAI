"""Auto-create WorkspaceAIConfig on Workspace creation.

Secrets are sourced from the environment ONLY (do not commit keys to
this file). Set via deploy-local/.env (gitignored) and propagated to
api/worker containers by docker-compose.planeai-runtime.yml.

NB: skipped when PYTEST_CURRENT_TEST is set, otherwise the signal
would create a config row before fixtures like make_ai_config
get a chance to insert their own, causing a duplicate-PK error in
the tests we ship.
"""
import logging
import os

from django.apps import apps
from django.db.models.signals import post_save

logger = logging.getLogger("planeai.runtime.autoconfig_workspace")

# Read on import; container restart picks up rotated keys.
DEEPSEEK_KEY = os.environ.get("PLANEAI_DEEPSEEK_KEY") or os.environ.get(
    "ANTHROPIC_API_KEY", ""
)
OPENAI_KEY = os.environ.get("PLANEAI_OPENAI_KEY") or os.environ.get(
    "OPENAI_API_KEY", ""
)
CHAT_MODEL = os.environ.get("PLANEAI_CHAT_MODEL", "deepseek-v4-flash")
EMBED_MODEL = os.environ.get("PLANEAI_EMBED_MODEL", "text-embedding-3-small")
MONTHLY_BUDGET = int(os.environ.get("PLANEAI_MONTHLY_TOKEN_BUDGET", 5_000_000))


def _has_real_keys() -> bool:
    placeholders = {"", "CHANGE_ME", "REPLACE_ME"}
    return DEEPSEEK_KEY not in placeholders and OPENAI_KEY not in placeholders


def _on_workspace_created(sender, instance, created, **kwargs):
    if not created:
        return
    if os.environ.get("PYTEST_CURRENT_TEST"):
        # never auto-configure during tests — fixtures own that
        return
    if not _has_real_keys():
        logger.warning(
            "skipping workspace %s autoconfig: PLANEAI_DEEPSEEK_KEY / "
            "PLANEAI_OPENAI_KEY not set in container env",
            getattr(instance, "slug", instance.pk),
        )
        return
    from ai.models import WorkspaceAIConfig

    WorkspaceAIConfig.objects.update_or_create(
        workspace=instance,
        defaults={
            "anthropic_key": DEEPSEEK_KEY,
            "openai_key": OPENAI_KEY,
            "chat_model": CHAT_MODEL,
            "embed_model": EMBED_MODEL,
            "monthly_token_budget": MONTHLY_BUDGET,
            "enabled": True,
        },
    )


def connect():
    Workspace = apps.get_model("db", "Workspace")
    post_save.connect(
        _on_workspace_created,
        sender=Workspace,
        dispatch_uid="planeai.autoconfig_workspace",
    )
    if not _has_real_keys():
        logger.warning(
            "autoconfig_workspace connected but PLANEAI_DEEPSEEK_KEY / "
            "PLANEAI_OPENAI_KEY are missing — new workspaces will NOT "
            "be auto-enabled. Set them in deploy-local/.env."
        )
