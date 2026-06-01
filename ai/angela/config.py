"""Sandbox registry + safety guards for Angela.

A *target* is an allow-listed sandbox repository Angela is permitted to
work on. The client never passes a clone URL — it passes a logical key
(``"demo"``) which we resolve here against Django settings. This is the
single chokepoint that keeps Angela away from the user's prod repo and
away from this planeAI codebase.

Settings shape (``settings.ANGELA``)::

    ANGELA = {
        "WORKDIR": "/tmp/angela",          # all checkouts live here
        "DEFAULT_TARGET": "demo",
        "TARGETS": {
            "demo": {
                "clone_url": "https://github.com/ne4ek/autodoc.git",
                "default_branch": "main",
                "language": "python",
                "test_cmd": "python -m pytest -q",
                "install_cmd": "pip install -r requirements.txt",
                "staging_deploy_cmd": "bash deploy/staging.sh",
                "prod_deploy_cmd": "bash deploy/prod.sh",
                "staging_url": "http://localhost:8090",
                "prod_url": "http://localhost:8091",
            },
        },
        "WIKI": {
            "base_url": "http://localhost:8080",   # local MediaWiki on the PC
            "username": "Angela",
            "password": "",                         # injected via env
            "enabled": True,
        },
        "MAX_FIX_ITERATIONS": 3,
        "CMD_TIMEOUT": 600,
    }

Everything has a conservative default so the module imports and the
unit tests run without a populated settings block.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from django.conf import settings


class AngelaConfigError(Exception):
    """Raised when a target key is unknown or a path escapes the sandbox."""


@dataclass(frozen=True)
class TargetConfig:
    key: str
    clone_url: str
    default_branch: str = "main"
    language: str = "python"
    test_cmd: str = "python -m pytest -q"
    install_cmd: str = ""
    staging_deploy_cmd: str = ""
    prod_deploy_cmd: str = ""
    staging_url: str = ""
    prod_url: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


_DEFAULTS: dict[str, Any] = {
    "WORKDIR": os.path.join(os.environ.get("ANGELA_WORKDIR", "") or "/tmp/angela"),
    # Where successful runs publish their built artifact (a shared volume
    # also mounted into the static-hosting nginx) + the public URL base
    # that serves it. When URL base is set, every successful deploy yields
    # a real clickable link instead of a dry-run no-op.
    "ARTIFACTS_DIR": os.environ.get("ANGELA_ARTIFACTS_DIR", "") or "/srv/angela-artifacts",
    "ARTIFACTS_URL_BASE": os.environ.get("ANGELA_ARTIFACTS_URL_BASE", ""),
    "DEFAULT_TARGET": "demo",
    "TARGETS": {
        # Ships pointing at the autodoc demo repo so a fresh install has
        # *something* to exercise. Override in settings for real use.
        "demo": {
            "clone_url": os.environ.get(
                "ANGELA_DEMO_CLONE_URL",
                "https://github.com/ne4ek/autodoc.git",
            ),
            "default_branch": "main",
            "language": "python",
            "test_cmd": "python -m pytest -q",
            "install_cmd": "pip install -r requirements.txt",
            "staging_deploy_cmd": "",
            "prod_deploy_cmd": "",
            "staging_url": "http://localhost:8090",
            "prod_url": "http://localhost:8091",
        },
    },
    "WIKI": {
        "base_url": os.environ.get("ANGELA_WIKI_URL", "http://localhost:8080"),
        "username": os.environ.get("ANGELA_WIKI_USER", "Angela"),
        "password": os.environ.get("ANGELA_WIKI_PASSWORD", ""),
        "enabled": True,
    },
    "MAX_FIX_ITERATIONS": 3,
    "CMD_TIMEOUT": 600,
}


def _raw() -> dict[str, Any]:
    cfg = dict(_DEFAULTS)
    override = getattr(settings, "ANGELA", None)
    if isinstance(override, dict):
        # shallow merge top-level, deep-ish merge TARGETS/WIKI
        for k, v in override.items():
            if k in ("TARGETS", "WIKI") and isinstance(v, dict):
                merged = dict(cfg.get(k, {}))
                merged.update(v)
                cfg[k] = merged
            else:
                cfg[k] = v
    return cfg


def workdir() -> Path:
    """Base directory all sandbox checkouts live under. Created lazily."""
    p = Path(_raw()["WORKDIR"]).resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def artifacts_dir() -> Path:
    """Filesystem dir where successful runs publish their artifact.
    Created lazily; world-writable expectations are handled at deploy."""
    p = Path(_raw().get("ARTIFACTS_DIR", "/srv/angela-artifacts")).resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def artifacts_url_base() -> str:
    """Public URL base that serves ``artifacts_dir`` (no trailing slash).
    Empty string disables artifact hosting (deploy falls back to dry-run)."""
    return str(_raw().get("ARTIFACTS_URL_BASE", "")).rstrip("/")


def max_fix_iterations() -> int:
    return int(_raw().get("MAX_FIX_ITERATIONS", 3))


def cmd_timeout() -> int:
    return int(_raw().get("CMD_TIMEOUT", 600))


def default_target() -> str:
    return str(_raw().get("DEFAULT_TARGET", "demo"))


def resolve_target(key: str | None) -> TargetConfig:
    """Map a logical target key to its :class:`TargetConfig`.

    Raises :class:`AngelaConfigError` for an unknown key — this is the
    guard that prevents a client from pointing Angela at an arbitrary
    repository.
    """
    raw = _raw()
    key = key or raw.get("DEFAULT_TARGET", "demo")
    targets = raw.get("TARGETS", {})
    if key not in targets:
        raise AngelaConfigError(
            f"unknown Angela target '{key}'. Allowed: {sorted(targets)}"
        )
    t = dict(targets[key])
    return TargetConfig(
        key=key,
        clone_url=t.get("clone_url", ""),
        default_branch=t.get("default_branch", "main"),
        language=t.get("language", "python"),
        test_cmd=t.get("test_cmd", "python -m pytest -q"),
        install_cmd=t.get("install_cmd", ""),
        staging_deploy_cmd=t.get("staging_deploy_cmd", ""),
        prod_deploy_cmd=t.get("prod_deploy_cmd", ""),
        staging_url=t.get("staging_url", ""),
        prod_url=t.get("prod_url", ""),
        extra={
            k: v
            for k, v in t.items()
            if k
            not in {
                "clone_url",
                "default_branch",
                "language",
                "test_cmd",
                "install_cmd",
                "staging_deploy_cmd",
                "prod_deploy_cmd",
                "staging_url",
                "prod_url",
            }
        },
    )


def list_targets() -> list[str]:
    return sorted(_raw().get("TARGETS", {}).keys())


def wiki_config() -> dict[str, Any]:
    return dict(_raw().get("WIKI", {}))


def assert_inside_sandbox(path: str | Path) -> Path:
    """Guarantee ``path`` resolves to a location under :func:`workdir`.

    Defence-in-depth against path traversal in a model-generated file
    path (``../../etc/passwd``). Every filesystem write in sandbox.py
    funnels through here.
    """
    base = workdir()
    resolved = (base / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
    try:
        resolved.relative_to(base)
    except ValueError as exc:
        raise AngelaConfigError(
            f"path '{path}' escapes the Angela sandbox ({base})"
        ) from exc
    return resolved
