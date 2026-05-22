"""Shared fixtures for ai/ tests.

Pytest-django plus a deterministic embedding mock (vector derived
from a content hash, not random) so the tests are reproducible. The
vector dimension is pinned to 1536 — anything else would fail at
``pgvector`` insertion time.
"""

from __future__ import annotations

import hashlib
import uuid
from unittest.mock import MagicMock

import pytest

from ai import providers


@pytest.fixture
def make_user(db):
    from plane.db.models import User

    def _make(handle: str = "u"):
        return User.objects.create(
            email=f"{handle}+{uuid.uuid4().hex[:6]}@example.test",
            username=f"{handle}-{uuid.uuid4().hex[:6]}",
            first_name=handle,
            is_password_autoset=True,
        )

    return _make


@pytest.fixture
def make_workspace(db, make_user):
    from plane.db.models import Workspace

    def _make(slug: str | None = None, owner=None):
        owner = owner or make_user("owner")
        slug = slug or f"ws-{uuid.uuid4().hex[:8]}"
        return Workspace.objects.create(name=slug, slug=slug, owner=owner)

    return _make


@pytest.fixture
def make_workspace_member(db):
    from plane.db.models import WorkspaceMember

    def _make(*, workspace, user, role=20, is_active=True):
        return WorkspaceMember.objects.create(
            workspace=workspace, member=user, role=role, is_active=is_active
        )

    return _make


@pytest.fixture
def make_project(db, make_user):
    from plane.db.models import Project

    def _make(*, workspace, identifier=None, created_by=None, exclude_from_ai=False):
        from ai.models import AIProjectSettings

        identifier = identifier or f"P{uuid.uuid4().hex[:4]}"
        prj = Project.objects.create(
            workspace=workspace,
            name=identifier,
            identifier=identifier,
            created_by=created_by or workspace.owner,
        )
        if exclude_from_ai:
            AIProjectSettings.objects.create(project=prj, exclude_from_ai=True)
        return prj

    return _make


@pytest.fixture
def make_ai_config(db):
    from ai.models import WorkspaceAIConfig

    def _make(workspace, *, enabled=True, monthly_token_budget=1_000_000):
        return WorkspaceAIConfig.objects.create(
            workspace=workspace,
            enabled=enabled,
            anthropic_key="sk-ant-test",
            openai_key="sk-test",
            monthly_token_budget=monthly_token_budget,
        )

    return _make


@pytest.fixture
def make_issue(db):
    from plane.db.models import Issue

    def _make(*, workspace, project, name="issue", description="", created_by=None):
        return Issue.objects.create(
            workspace=workspace,
            project=project,
            name=name,
            description_stripped=description,
            created_by=created_by or workspace.owner,
        )

    return _make


def deterministic_vector(content: str) -> list[float]:
    """Return a deterministic 1536-d vector seeded by sha256(content)."""
    h = hashlib.sha256(content.encode("utf-8")).digest()
    # Tile the 32-byte digest across 1536 floats in [0, 1).
    vec = []
    for i in range(1536):
        vec.append(h[i % 32] / 255.0)
    return vec


@pytest.fixture
def make_chunk(db):
    from ai.models import DocumentChunk

    def _make(
        *,
        workspace,
        project=None,
        source_type="work_item",
        source_id=None,
        chunk_index=0,
        content="hello",
    ):
        source_id = source_id or uuid.uuid4()
        h = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return DocumentChunk.objects.create(
            workspace=workspace,
            project=project,
            source_type=source_type,
            source_id=source_id,
            chunk_index=chunk_index,
            content=content,
            token_count=len(content.split()),
            embedding=deterministic_vector(content),
            content_hash=h,
        )

    return _make


@pytest.fixture
def mock_embed(monkeypatch):
    """Replace providers.OpenAIEmbed with a deterministic stand-in.

    Returns the fake class so a test can inspect ``calls`` if it wants
    to count embed() invocations.
    """

    class _FakeEmbed:
        calls: list[int] = []

        def __init__(self, api_key, model="text-embedding-3-small"):
            self.api_key = api_key
            self.model = model

        def embed(self, texts):
            _FakeEmbed.calls.append(len(texts))
            vectors = [deterministic_vector(t) for t in texts]
            tokens = sum(len(t) for t in texts)
            return vectors, tokens

    _FakeEmbed.calls = []
    monkeypatch.setattr(providers, "OpenAIEmbed", _FakeEmbed)
    return _FakeEmbed


@pytest.fixture
def mock_claude(monkeypatch):
    """Replace providers.anthropic.Anthropic with a MagicMock so
    tests can configure ``messages.create.return_value`` per test."""
    fake = MagicMock()
    monkeypatch.setattr(providers.anthropic, "Anthropic", lambda **kw: fake)
    return fake
