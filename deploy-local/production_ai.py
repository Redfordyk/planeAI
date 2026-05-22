# Plane settings shim — extends plane.settings.production with the
# `ai` Django app (TZ 0.5). Use by setting
# DJANGO_SETTINGS_MODULE=plane.settings.production_ai on api / worker /
# beat-worker / migrator containers.

from .production import *  # noqa: F401,F403
from .production import INSTALLED_APPS

if "ai" not in INSTALLED_APPS:
    INSTALLED_APPS = list(INSTALLED_APPS) + ["ai"]
