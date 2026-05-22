"""Replacement ROOT_URLCONF for planeAI.

Extends Plane's upstream `plane.urls.urlpatterns` with our routes
mounted under `/api/ai/`, without touching any upstream file. Set
``ROOT_URLCONF=ai._root_urls`` in the settings shim (see
[deploy-local/production_ai.py](../deploy-local/production_ai.py)).
"""

from __future__ import annotations

from django.urls import include, path

from plane.urls import urlpatterns as _plane_urls


urlpatterns = list(_plane_urls) + [
    path("api/ai/", include("ai.urls", namespace="ai")),
]
