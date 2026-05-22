"""AI add-on data model.

Four tables, all in our own `ai` schema. We never add columns to
Plane's models (see CLAUDE.md invariant 6) — `AIProjectSettings` is
intentionally a separate table rather than a flag on `db.Project`.

Model name resolutions per [SCHEMA.md](../SCHEMA.md):
    - workspace  -> db.Workspace   (table `workspaces`)
    - project    -> db.Project     (table `projects`)
    - user       -> db.User        (Plane's custom user, NOT auth.User)

Migration that creates these tables + pgvector extension + HNSW index
lives separately (TZ 1.2 / ai/migrations/0001_initial.py). This module
is import-clean: pulling it in must not trigger any DDL.
"""

from __future__ import annotations

from uuid import uuid4

from django.db import models
from encrypted_model_fields.fields import EncryptedCharField
from pgvector.django import HnswIndex, VectorField


# `text-embedding-3-small` returns 1536 floats. Pinned here so a future
# model swap is loud (we change the constant, migrations follow).
EMBEDDING_DIM = 1536


class DocumentChunk(models.Model):
    """One indexable text chunk + its embedding.

    `project` is nullable because `db.Page` has no direct FK to Project
    (SCHEMA.md §db.Page) — pages live at workspace level and link to
    projects through `db.ProjectPage`. For pages we either store the
    chunk with `project=NULL` and filter via ProjectPage at query time,
    or fan out one chunk row per project — decision deferred to the
    page-indexing implementation (TZ 1.5). The schema supports both.
    """

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    workspace = models.ForeignKey(
        "db.Workspace",
        on_delete=models.CASCADE,
        related_name="ai_chunks",
    )
    project = models.ForeignKey(
        "db.Project",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="ai_chunks",
    )

    SOURCE_WORK_ITEM = "work_item"
    SOURCE_COMMENT = "comment"
    SOURCE_PAGE = "page"
    SOURCE_CHOICES = (
        (SOURCE_WORK_ITEM, "work_item"),
        (SOURCE_COMMENT, "comment"),
        (SOURCE_PAGE, "page"),
    )
    source_type = models.CharField(max_length=20, choices=SOURCE_CHOICES)
    source_id = models.UUIDField()

    chunk_index = models.IntegerField()
    content = models.TextField()
    token_count = models.IntegerField()
    embedding = VectorField(dimensions=EMBEDDING_DIM)
    # SHA-256 hex of the source slice that produced this chunk. Lets us
    # skip re-embedding when nothing changed (TZ 1.5 reindex logic).
    content_hash = models.CharField(max_length=64)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "ai_document_chunk"
        verbose_name = "Document chunk"
        verbose_name_plural = "Document chunks"
        indexes = [
            # HNSW with cosine distance, parameters from pgvector
            # defaults (m=16, ef_construction=64). vector_cosine_ops
            # because we normalise embeddings and want cosine
            # similarity, not L2.
            HnswIndex(
                name="ai_chunk_hnsw_idx",
                fields=["embedding"],
                m=16,
                ef_construction=64,
                opclasses=["vector_cosine_ops"],
            ),
            # Hot retrieval path: filter by workspace, optionally by
            # project, then ANN over embedding.
            models.Index(
                fields=["workspace", "project"],
                name="ai_chunk_ws_prj_idx",
            ),
            # Source dedupe lookup ("does a chunk for this issue
            # already exist?").
            models.Index(
                fields=["source_type", "source_id"],
                name="ai_chunk_src_idx",
            ),
        ]
        constraints = [
            # One chunk row per (source, chunk_index). Reindex
            # overwrites rather than duplicates.
            models.UniqueConstraint(
                fields=["source_type", "source_id", "chunk_index"],
                name="ai_chunk_unique_per_source",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.source_type}:{self.source_id} #{self.chunk_index}"


class WorkspaceAIConfig(models.Model):
    """Per-workspace AI configuration.

    API keys are stored encrypted at rest using
    django-encrypted-model-fields. The encryption key
    (`FIELD_ENCRYPTION_KEY`) is environment-injected — see
    [SECRETS.md](../SECRETS.md). If `enabled=False`, no AI feature
    should run for this workspace; the gate is checked in views.
    """

    workspace = models.OneToOneField(
        "db.Workspace",
        on_delete=models.CASCADE,
        related_name="ai_config",
        primary_key=True,
    )
    anthropic_key = EncryptedCharField(max_length=255, blank=True, default="")
    openai_key = EncryptedCharField(max_length=255, blank=True, default="")
    chat_model = models.CharField(max_length=60, default="claude-sonnet-4-6")
    embed_model = models.CharField(max_length=64, default="text-embedding-3-small")
    # 5M tokens/month — covers ~5k Claude Sonnet turns or ~500k chars
    # of embedding traffic at current pricing. Overrideable per ws.
    monthly_token_budget = models.BigIntegerField(default=5_000_000)
    enabled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "ai_workspace_config"
        verbose_name = "Workspace AI config"
        verbose_name_plural = "Workspace AI configs"

    def __str__(self) -> str:
        return f"AIConfig({self.workspace_id})"


class AIUsageLog(models.Model):
    """One row per LLM/embedding call. Drives both billing and the
    per-workspace `monthly_token_budget` enforcement (TZ 1.7).

    `cache_read_tokens` / `cache_creation_tokens` are populated when
    Anthropic prompt caching is used; OpenAI embeddings leave them
    zero.
    """

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    workspace = models.ForeignKey(
        "db.Workspace",
        on_delete=models.CASCADE,
        related_name="ai_usage_logs",
    )
    user = models.ForeignKey(
        "db.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ai_usage_logs",
    )

    FEATURE_INTENT_SEARCH = "intent_search"
    FEATURE_SUMMARIZE = "summarize"
    FEATURE_BULK = "bulk"
    FEATURE_AGENT = "agent"
    FEATURE_EMBED = "embed"
    FEATURE_CHOICES = (
        (FEATURE_INTENT_SEARCH, "intent_search"),
        (FEATURE_SUMMARIZE, "summarize"),
        (FEATURE_BULK, "bulk"),
        (FEATURE_AGENT, "agent"),
        (FEATURE_EMBED, "embed"),
    )
    feature = models.CharField(max_length=40, choices=FEATURE_CHOICES)
    model = models.CharField(max_length=60)

    input_tokens = models.IntegerField(default=0)
    output_tokens = models.IntegerField(default=0)
    cache_read_tokens = models.IntegerField(default=0)
    cache_creation_tokens = models.IntegerField(default=0)
    cost_usd = models.DecimalField(max_digits=10, decimal_places=6, default=0)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "ai_usage_log"
        verbose_name = "AI usage log"
        verbose_name_plural = "AI usage logs"
        indexes = [
            # Budget check query: SUM(tokens) WHERE workspace=? AND
            # created_at >= start_of_month.
            models.Index(
                fields=["workspace", "created_at"],
                name="ai_usage_ws_time_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.feature}/{self.model} @{self.created_at:%Y-%m-%d %H:%M}"


class AIAgent(models.Model):
    """Marker row for a Plane user that is in fact an AI agent.

    An agent is just a normal Plane ``db.User`` with:

      - a row here (``is_ai_agent`` semantics — we never add a column
        to ``db.User`` per CLAUDE.md invariant 6),
      - ``WorkspaceMember`` row(s) on its workspace with a NON-admin
        role (MEMBER, not ADMIN),
      - ``ProjectMember`` row(s) restricted to the specific projects
        the agent is allowed to act in (project-scope, not workspace-
        wide).

    The TZ 5.1 trigger uses this table to recognise that a save event
    on an Issue concerns an agent (because the agent is an assignee).
    The actual write capabilities are governed by the same ACL layer
    as humans (``ai/acl.py``) plus the white-list in the worker (TZ
    5.2). Disabling an agent by flipping ``enabled=False`` halts new
    triggers immediately without revoking its ProjectMember rows.
    """

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    user = models.OneToOneField(
        "db.User",
        on_delete=models.CASCADE,
        related_name="ai_agent",
    )
    workspace = models.ForeignKey(
        "db.Workspace",
        on_delete=models.CASCADE,
        related_name="ai_agents",
    )
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "ai_agent"
        verbose_name = "AI agent"
        verbose_name_plural = "AI agents"
        indexes = [
            # Hot path of the trigger: "is this user id an enabled
            # agent in this workspace?".
            models.Index(
                fields=["workspace", "enabled"],
                name="ai_agent_ws_enabled_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"AIAgent(user={self.user_id}, ws={self.workspace_id})"


class AIProjectSettings(models.Model):
    """Per-project AI opt-out flag.

    Lives in its own table — we never add columns to `db.Project`
    (CLAUDE.md invariant 6). Projects without a row are treated as
    `exclude_from_ai=False` (default behaviour).
    """

    project = models.OneToOneField(
        "db.Project",
        on_delete=models.CASCADE,
        related_name="ai_settings",
        primary_key=True,
    )
    exclude_from_ai = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "ai_project_settings"
        verbose_name = "AI project settings"
        verbose_name_plural = "AI project settings"

    def __str__(self) -> str:
        return f"AISettings({self.project_id}, exclude={self.exclude_from_ai})"
