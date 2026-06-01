# Plane settings shim — extends plane.settings.production with the
# `ai` Django app (TZ 0.5). Use by setting
# DJANGO_SETTINGS_MODULE=plane.settings.production_ai on api / worker /
# beat-worker / migrator containers.

import os

from .production import *  # noqa: F401,F403
from .production import INSTALLED_APPS

if "ai" not in INSTALLED_APPS:
    INSTALLED_APPS = list(INSTALLED_APPS) + ["ai"]

# Override Plane's ROOT_URLCONF with our wrapper that mounts /api/ai/.
# `ai._root_urls` imports `plane.urls.urlpatterns` and extends it —
# so all upstream routes keep working, we just add ours alongside.
ROOT_URLCONF = "ai._root_urls"

# Required by django-encrypted-model-fields at import time. The package
# raises ImproperlyConfigured if the value is missing or empty — that's
# the correct behaviour for prod (no silent fallback to a hardcoded
# default). Generate per environment via `python scripts/gen_encryption_key.py`
# and inject through the container's environment.
FIELD_ENCRYPTION_KEY = os.environ.get("FIELD_ENCRYPTION_KEY", "")

# Celery 6.0 deprecation: broker_connection_retry no longer governs
# startup-time retries by default. Silence the noisy warning while
# preserving the current "retry on startup" behaviour we want.
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True

# planeAI: allow any password during sign-up / password-change.
# Disables Django's built-in checks (8-char min, common-password
# blocklist, numeric-only blocklist, similar-to-attribute). The
# remaining strength check (zxcvbn score >= 3) lives inside Plane's
# auth adapter; we neutralise it in ai-runtime/permissive_password.py.
AUTH_PASSWORD_VALIDATORS = []

# --- Angela: autonomous coding agent (sandbox) -----------------------
# All values are env-driven so the same image works across dev/staging.
# Angela only ever touches an allow-listed sandbox repo (never the
# user's prod, never this codebase). The MediaWiki target is expected
# to run LOCALLY on a developer PC, not on the server — leave
# ANGELA_WIKI_PASSWORD empty in server environments to keep docs
# publishing disabled there.
ANGELA = {
    "WORKDIR": os.environ.get("ANGELA_WORKDIR", "/tmp/angela"),
    "DEFAULT_TARGET": os.environ.get("ANGELA_DEFAULT_TARGET", "demo"),
    "MAX_FIX_ITERATIONS": int(os.environ.get("ANGELA_MAX_FIX_ITERATIONS", "3")),
    "CMD_TIMEOUT": int(os.environ.get("ANGELA_CMD_TIMEOUT", "600")),
    "TARGETS": {
        "demo": {
            "clone_url": os.environ.get(
                "ANGELA_DEMO_CLONE_URL", "https://github.com/ne4ek/autodoc.git"
            ),
            "default_branch": os.environ.get("ANGELA_DEMO_BRANCH", "main"),
            "language": "python",
            "test_cmd": os.environ.get("ANGELA_DEMO_TEST_CMD", "python -m pytest -q"),
            "install_cmd": os.environ.get(
                "ANGELA_DEMO_INSTALL_CMD", "pip install -r requirements.txt"
            ),
            "staging_deploy_cmd": os.environ.get("ANGELA_DEMO_STAGING_CMD", ""),
            "prod_deploy_cmd": os.environ.get("ANGELA_DEMO_PROD_CMD", ""),
            "staging_url": os.environ.get("ANGELA_DEMO_STAGING_URL", ""),
            "prod_url": os.environ.get("ANGELA_DEMO_PROD_URL", ""),
        },
    },
    "WIKI": {
        "base_url": os.environ.get("ANGELA_WIKI_URL", "http://localhost:8080"),
        "username": os.environ.get("ANGELA_WIKI_USER", "Angela"),
        "password": os.environ.get("ANGELA_WIKI_PASSWORD", ""),
        "enabled": os.environ.get("ANGELA_WIKI_ENABLED", "true").lower() == "true",
    },
}
