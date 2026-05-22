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
