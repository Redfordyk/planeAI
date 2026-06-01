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
    # planeAI orchestrator kill-switch (TZ 11.2). When True, the
    # multi-agent dispatcher refuses to schedule any new event — runs
    # already in flight finish, nothing new starts. Flipped from the UI
    # or `manage.py ai_kill_switch`. Does NOT touch ``enabled`` so the
    # base AI features (search, voice) keep working.
    agents_killed = models.BooleanField(default=False)
    # Per-workspace cap on agent ACTIONS (writes) per rolling hour. The
    # circuit breaker compares AgentAction count over the last 1h to
    # this value and refuses new actions when it's hit, until the hour
    # rolls forward or the breaker is manually reset. Independent of
    # the token budget — protects against runaway action loops even
    # when each action is cheap.
    max_agent_actions_per_hour = models.IntegerField(default=60)
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


class AIAgentActionLog(models.Model):
    """Append-only audit trail for every agent action attempt.

    TZ 5.2 DoD requires "каждое действие пишется в аудит-лог" — the
    table answers two questions during an incident:

      - what did the agent try to do? (``tool_name`` + ``input``)
      - was it allowed through? (``status``, ``error``)

    We log BOTH applied and rejected actions. A rejection (e.g.
    cross-project target) is the more interesting case — it tells us
    the model attempted something the white-list / scope guard
    caught, which is exactly the kind of event the safety review
    needs to see.

    The row is intentionally narrow: structured JSON in ``input`` so
    we don't need a column per tool. ``output`` carries whatever the
    handler returned (e.g. "added 2 labels"). Never store API keys
    or user-supplied free text from the issue body here — that
    duplicates Plane's storage and creates a GDPR exposure.
    """

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    agent = models.ForeignKey(
        "ai.AIAgent",
        on_delete=models.CASCADE,
        related_name="action_logs",
    )
    workspace = models.ForeignKey(
        "db.Workspace",
        on_delete=models.CASCADE,
        related_name="ai_agent_action_logs",
    )
    # `project` is the scope the action was bound to. NOT nullable —
    # agent actions are project-scoped by design (TZ 5.2 invariant 2).
    project = models.ForeignKey(
        "db.Project",
        on_delete=models.CASCADE,
        related_name="ai_agent_action_logs",
    )
    issue_id = models.UUIDField()
    tool_name = models.CharField(max_length=40)
    input = models.JSONField(default=dict)
    output = models.JSONField(default=dict, blank=True)

    STATUS_APPLIED = "applied"
    STATUS_REJECTED = "rejected"  # white-list / scope guard refused
    STATUS_ERROR = "error"  # handler raised
    STATUS_CHOICES = (
        (STATUS_APPLIED, "applied"),
        (STATUS_REJECTED, "rejected"),
        (STATUS_ERROR, "error"),
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)
    error = models.TextField(blank=True, default="")

    # When the TZ 5.6 undo endpoint rolls back this action, we stamp
    # the timestamp here rather than deleting / mutating the row.
    # The audit trail must remain append-only — "deleted" wouldn't
    # answer "did this action exist?" during a later incident review.
    # NULL = still in effect; a value = undone at that moment.
    undone_at = models.DateTimeField(null=True, blank=True)
    # User who performed the undo. SET_NULL so account deletion does
    # not orphan the audit row (we keep the timestamp regardless).
    undone_by = models.ForeignKey(
        "db.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ai_agent_actions_undone",
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "ai_agent_action_log"
        verbose_name = "AI agent action"
        verbose_name_plural = "AI agent actions"
        indexes = [
            # Per-issue audit lookup ("what did the agent do on this
            # issue?").
            models.Index(
                fields=["issue_id", "created_at"],
                name="ai_agent_log_issue_idx",
            ),
            # Per-workspace incident review.
            models.Index(
                fields=["workspace", "created_at"],
                name="ai_agent_log_ws_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.tool_name}/{self.status}@{self.issue_id}"


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


class IssueSummary(models.Model):
    """Cached AI summary of a work item (title + description + comments).

    One row per work item — the row is overwritten when the source
    content changes. ``content_hash`` is a sha256 of the title +
    description + every comment in stable order; a cache hit is when
    the hash on a new request matches the stored hash, in which case
    we skip the LLM call entirely (TZ 3.2 — P0.3 backlog).

    We store ``workspace_id`` denormalised so cleanup jobs and
    workspace-scoped queries don't need to traverse Plane's Issue
    table (CLAUDE.md invariant 6: own data lives in our tables).
    No FK to ``db.Issue`` either — when an issue is hard-deleted upstream
    we cleanup via signal in ai/signals.py.
    """

    id = models.BigAutoField(primary_key=True)
    issue_id = models.UUIDField(unique=True, db_index=True)
    workspace_id = models.UUIDField(db_index=True)
    content_hash = models.CharField(max_length=64)
    summary_text = models.TextField()
    model_used = models.CharField(max_length=60)
    # Token counters are informational — biller of record is AIUsageLog.
    input_tokens = models.IntegerField(default=0)
    output_tokens = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "ai_issue_summary"
        verbose_name = "AI issue summary"
        verbose_name_plural = "AI issue summaries"
        indexes = [
            models.Index(fields=["workspace_id", "updated_at"]),
        ]

    def __str__(self) -> str:
        return f"IssueSummary(issue={self.issue_id}, model={self.model_used})"


# ---------------------------------------------------------------------------
# planeAI Multi-Agent Orchestrator (phases 7-12)
# ---------------------------------------------------------------------------
#
# Four tables that let a system of agents drive a project from a
# user-stated goal to completion. They live in our `ai` schema (never
# add columns to Plane models — CLAUDE.md invariant 6).
#
#   ProjectGoal     — user-stated outcome ("ship MVP by July 15")
#   AgentAction     — append-only audit of every agentic decision
#   PredictedRisk   — risk row produced by MONITOR (delay/blocker/...)
#   TeamVelocity    — completed-issue stats feeding ANALYST + MONITOR
#
# All four carry workspace_id so isolation (CLAUDE.md invariant 1) is
# enforced at the DB level, not just at the view layer.


class ProjectGoal(models.Model):
    """A user-stated outcome the orchestrator drives toward.

    The goal owns the plan tree (issues created by PLANNER point back
    through their project + the goal's project_id) and the timeline
    (deadline). State machine is intentionally tiny:

        draft → planning → executing → at_risk ↔ executing → done
                                              ↘ blocked

    `constraints` is freeform JSON for budget, team size, MVP-vs-full
    scope, allowed_freelancers, etc. PLANNER reads it as advisory
    context, not as machine-verified facts.
    """

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    workspace = models.ForeignKey(
        "db.Workspace",
        on_delete=models.CASCADE,
        related_name="ai_goals",
    )
    project = models.ForeignKey(
        "db.Project",
        on_delete=models.CASCADE,
        related_name="ai_goals",
        null=True,
        blank=True,
    )
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, default="")
    deadline = models.DateField(null=True, blank=True)
    constraints = models.JSONField(default=dict, blank=True)

    STATUS_DRAFT = "draft"
    STATUS_PLANNING = "planning"
    STATUS_EXECUTING = "executing"
    STATUS_AT_RISK = "at_risk"
    STATUS_BLOCKED = "blocked"
    STATUS_DONE = "done"
    STATUS_CHOICES = (
        (STATUS_DRAFT, "draft"),
        (STATUS_PLANNING, "planning"),
        (STATUS_EXECUTING, "executing"),
        (STATUS_AT_RISK, "at_risk"),
        (STATUS_BLOCKED, "blocked"),
        (STATUS_DONE, "done"),
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_DRAFT)
    # PLANNER stores the JSON tree (epics → tasks → subtasks) before
    # creating issues, so the UI can preview before the user confirms.
    plan_preview = models.JSONField(default=dict, blank=True)
    # IDs of Issues actually created from the plan. JSON list of UUIDs.
    plan_issue_ids = models.JSONField(default=list, blank=True)

    created_by = models.ForeignKey(
        "db.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ai_goals_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "ai_project_goal"
        verbose_name = "AI project goal"
        verbose_name_plural = "AI project goals"
        indexes = [
            models.Index(
                fields=["workspace", "status"],
                name="ai_goal_ws_status_idx",
            ),
            models.Index(
                fields=["deadline"],
                name="ai_goal_deadline_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"Goal({self.title[:40]})"


class AgentAction(models.Model):
    """Append-only record of every decision the multi-agent system
    made. Distinct from AIAgentActionLog (TZ 5.2) — that one logs
    *tool* calls of the single-issue agent worker; THIS one logs
    higher-level orchestrator decisions (route an event, decompose a
    goal, escalate a risk).

    `agent_type` identifies which of the 7 agents fired
    (PLANNER/MONITOR/EXECUTOR/ESCALATOR/ANALYST/COMMUNICATOR/
    ORCHESTRATOR). `risk_level` mirrors the Decision Layer matrix
    (AUTO/NOTIFY/CONFIRM/ESCALATE) — useful for filtering the activity
    feed by "things the system did on its own" vs "things waiting on
    a human".

    `reasoning` is the model's short rationale ("assigning to Vova:
    lowest current load, has done iOS UI before"). Cap at ~1KB; if
    you need more, link to a separate page.
    """

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    workspace = models.ForeignKey(
        "db.Workspace",
        on_delete=models.CASCADE,
        related_name="ai_agent_actions",
    )
    project = models.ForeignKey(
        "db.Project",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="ai_agent_actions",
    )
    goal = models.ForeignKey(
        "ai.ProjectGoal",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="actions",
    )
    # Target Issue (when the action touched one). Loose UUID — Plane's
    # Issue may be soft-deleted later and we don't want the audit row
    # to vanish.
    target_issue_id = models.UUIDField(null=True, blank=True)

    AGENT_PLANNER = "PLANNER"
    AGENT_MONITOR = "MONITOR"
    AGENT_EXECUTOR = "EXECUTOR"
    AGENT_ESCALATOR = "ESCALATOR"
    AGENT_ANALYST = "ANALYST"
    AGENT_COMMUNICATOR = "COMMUNICATOR"
    AGENT_ORCHESTRATOR = "ORCHESTRATOR"
    AGENT_CHOICES = (
        (AGENT_PLANNER, "PLANNER"),
        (AGENT_MONITOR, "MONITOR"),
        (AGENT_EXECUTOR, "EXECUTOR"),
        (AGENT_ESCALATOR, "ESCALATOR"),
        (AGENT_ANALYST, "ANALYST"),
        (AGENT_COMMUNICATOR, "COMMUNICATOR"),
        (AGENT_ORCHESTRATOR, "ORCHESTRATOR"),
    )
    agent_type = models.CharField(max_length=20, choices=AGENT_CHOICES)
    action_type = models.CharField(max_length=60)
    input = models.JSONField(default=dict, blank=True)
    output = models.JSONField(default=dict, blank=True)
    reasoning = models.TextField(blank=True, default="")

    RISK_AUTO = "AUTO"
    RISK_NOTIFY = "NOTIFY"
    RISK_CONFIRM = "CONFIRM"
    RISK_ESCALATE = "ESCALATE"
    RISK_CHOICES = (
        (RISK_AUTO, "AUTO"),
        (RISK_NOTIFY, "NOTIFY"),
        (RISK_CONFIRM, "CONFIRM"),
        (RISK_ESCALATE, "ESCALATE"),
    )
    risk_level = models.CharField(max_length=12, choices=RISK_CHOICES, default=RISK_AUTO)

    STATUS_PROPOSED = "proposed"
    STATUS_APPLIED = "applied"
    STATUS_REJECTED = "rejected"
    STATUS_AWAITING_USER = "awaiting_user"
    STATUS_FAILED = "failed"
    STATUS_CHOICES = (
        (STATUS_PROPOSED, "proposed"),
        (STATUS_APPLIED, "applied"),
        (STATUS_REJECTED, "rejected"),
        (STATUS_AWAITING_USER, "awaiting_user"),
        (STATUS_FAILED, "failed"),
    )
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_APPLIED)
    approved_by_user = models.ForeignKey(
        "db.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ai_actions_approved",
    )

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "ai_agent_action"
        verbose_name = "AI agent action (orchestrator)"
        verbose_name_plural = "AI agent actions (orchestrator)"
        indexes = [
            models.Index(
                fields=["workspace", "created_at"],
                name="ai_act_ws_time_idx",
            ),
            models.Index(
                fields=["agent_type", "status"],
                name="ai_act_agent_status_idx",
            ),
            models.Index(
                fields=["target_issue_id"],
                name="ai_act_issue_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.agent_type}/{self.action_type}@{self.created_at:%H:%M}"


class PredictedRisk(models.Model):
    """A risk the MONITOR detected for one issue.

    Confidence is a float in [0, 1] — heuristic version reports a
    coarse 0.5/0.7/0.9 ladder; ML upgrade (LightGBM later) will fill
    it densely. `suggested_actions` is JSON the ESCALATOR consumes
    when surfacing options to the PM ("hire freelancer / simplify
    scope / move deadline").

    Risks are dedupe'd by (issue_id, risk_type, resolved=False) — a
    second MONITOR pass on the same issue UPDATES the existing row
    rather than creating a duplicate.
    """

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    workspace = models.ForeignKey(
        "db.Workspace",
        on_delete=models.CASCADE,
        related_name="ai_risks",
    )
    project = models.ForeignKey(
        "db.Project",
        on_delete=models.CASCADE,
        related_name="ai_risks",
    )
    issue_id = models.UUIDField()
    goal = models.ForeignKey(
        "ai.ProjectGoal",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="risks",
    )

    TYPE_DELAY = "delay"
    TYPE_BLOCKER = "blocker"
    TYPE_OVERLOAD = "overload"
    TYPE_DEPENDENCY = "dependency"
    TYPE_CHOICES = (
        (TYPE_DELAY, "delay"),
        (TYPE_BLOCKER, "blocker"),
        (TYPE_OVERLOAD, "overload"),
        (TYPE_DEPENDENCY, "dependency"),
    )
    risk_type = models.CharField(max_length=20, choices=TYPE_CHOICES)
    confidence = models.FloatField(default=0.5)

    IMPACT_LOW = "low"
    IMPACT_MEDIUM = "medium"
    IMPACT_HIGH = "high"
    IMPACT_CRITICAL = "critical"
    IMPACT_CHOICES = (
        (IMPACT_LOW, "low"),
        (IMPACT_MEDIUM, "medium"),
        (IMPACT_HIGH, "high"),
        (IMPACT_CRITICAL, "critical"),
    )
    impact = models.CharField(max_length=12, choices=IMPACT_CHOICES, default=IMPACT_MEDIUM)
    rationale = models.TextField(blank=True, default="")
    suggested_actions = models.JSONField(default=list, blank=True)
    resolved = models.BooleanField(default=False)
    escalated_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "ai_predicted_risk"
        verbose_name = "AI predicted risk"
        verbose_name_plural = "AI predicted risks"
        indexes = [
            models.Index(
                fields=["workspace", "resolved", "impact"],
                name="ai_risk_ws_open_idx",
            ),
            models.Index(
                fields=["issue_id", "risk_type"],
                name="ai_risk_issue_idx",
            ),
        ]
        constraints = [
            # One open risk per (issue, risk_type). Re-detection
            # updates the existing row.
            models.UniqueConstraint(
                fields=["issue_id", "risk_type"],
                condition=models.Q(resolved=False),
                name="ai_risk_unique_open",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.risk_type}/{self.impact}@{self.issue_id}"


class TeamVelocity(models.Model):
    """Per-user task completion stats feeding ANALYST + MONITOR.

    One row per completed Issue (recorded when issue.state transitions
    to a 'completed' group). `estimated_hours` / `actual_hours` are
    nullable because Plane's estimate column is optional — when both
    are present, ANALYST can compute over/under-estimate patterns.

    `task_type` is the most-prominent Plane label on the issue, or
    "uncategorised" — gives ANALYST a grouping dimension richer than
    raw user totals.
    """

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    workspace = models.ForeignKey(
        "db.Workspace",
        on_delete=models.CASCADE,
        related_name="ai_velocity",
    )
    project = models.ForeignKey(
        "db.Project",
        on_delete=models.CASCADE,
        related_name="ai_velocity",
    )
    user = models.ForeignKey(
        "db.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ai_velocity",
    )
    issue_id = models.UUIDField()
    task_type = models.CharField(max_length=60, default="uncategorised")
    estimated_hours = models.FloatField(null=True, blank=True)
    actual_hours = models.FloatField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "ai_team_velocity"
        verbose_name = "AI team velocity sample"
        verbose_name_plural = "AI team velocity samples"
        indexes = [
            models.Index(
                fields=["workspace", "completed_at"],
                name="ai_vel_ws_time_idx",
            ),
            models.Index(
                fields=["user", "task_type"],
                name="ai_vel_user_type_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["issue_id"],
                name="ai_vel_unique_per_issue",
            ),
        ]

    def __str__(self) -> str:
        return f"Velocity({self.user_id}, {self.task_type})"


# ---------------------------------------------------------------------------
# Angela — autonomous coding agent (sandbox-scoped)
# ---------------------------------------------------------------------------
#
# Angela takes a Plane Issue (or a freeform prompt) and runs a full
# code→self-review→test→deploy loop against an ISOLATED sandbox/demo
# repository (never the user's prod repo, never this planeAI codebase —
# see the deliberate scope decision: sandbox only). She can also emit
# project documentation to a (locally-hosted) MediaWiki.
#
# Two tables, both in our `ai` schema (CLAUDE.md invariant 6):
#
#   AngelaRun   — one end-to-end run, with the chosen deploy strategy
#   AngelaStep  — append-only per-phase log inside a run (for the feed)
#
# Both carry workspace_id so isolation (invariant 1) is enforced at the
# DB level, not just the view. The sandbox repo is identified by a
# logical key (`target_repo`), resolved to a concrete clone URL by
# settings (ai.angela.sandbox) — we never trust a client-supplied URL.


class AngelaRun(models.Model):
    """One autonomous Angela run over the sandbox repo.

    Lifecycle (``status``)::

        queued → coding → reviewing → testing ─┬→ deploying → succeeded
                    ↑__________________________│              ↘ failed
                       (bounded fix loop)       └→ awaiting_approval
                                                     (staging+gate mode,
                                                      prod deploy waits
                                                      for a human button)

    ``deploy_mode`` picks one of the three strategies surfaced as
    separate buttons in the UI. All three operate ONLY on the sandbox,
    so even ``autonomous_prod`` cannot touch a real production system.
    """

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    workspace = models.ForeignKey(
        "db.Workspace",
        on_delete=models.CASCADE,
        related_name="ai_angela_runs",
    )
    project = models.ForeignKey(
        "db.Project",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="ai_angela_runs",
    )
    # Plane Issue that seeded the run (when launched from a work item).
    # Loose UUID — the Issue may be soft-deleted later; the run history
    # must survive.
    issue_id = models.UUIDField(null=True, blank=True)

    # Logical sandbox key (e.g. "demo"), NOT a client URL. Resolved to a
    # concrete clone path/URL by ai.angela.sandbox.resolve_target().
    target_repo = models.CharField(max_length=120, default="demo")
    prompt = models.TextField(blank=True, default="")

    MODE_STAGING_GATE = "staging_gate"      # auto→staging, prod needs approval
    MODE_AUTONOMOUS_PROD = "autonomous_prod"  # auto all the way to (sandbox) prod
    MODE_MANUAL = "manual"                    # code+review+test, deploy by hand
    MODE_CHOICES = (
        (MODE_STAGING_GATE, "staging_gate"),
        (MODE_AUTONOMOUS_PROD, "autonomous_prod"),
        (MODE_MANUAL, "manual"),
    )
    deploy_mode = models.CharField(
        max_length=20, choices=MODE_CHOICES, default=MODE_STAGING_GATE
    )

    STATUS_QUEUED = "queued"
    STATUS_CODING = "coding"
    STATUS_REVIEWING = "reviewing"
    STATUS_TESTING = "testing"
    STATUS_DEPLOYING = "deploying"
    STATUS_AWAITING_APPROVAL = "awaiting_approval"
    STATUS_SUCCEEDED = "succeeded"
    STATUS_FAILED = "failed"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = (
        (STATUS_QUEUED, "queued"),
        (STATUS_CODING, "coding"),
        (STATUS_REVIEWING, "reviewing"),
        (STATUS_TESTING, "testing"),
        (STATUS_DEPLOYING, "deploying"),
        (STATUS_AWAITING_APPROVAL, "awaiting_approval"),
        (STATUS_SUCCEEDED, "succeeded"),
        (STATUS_FAILED, "failed"),
        (STATUS_CANCELLED, "cancelled"),
    )
    status = models.CharField(
        max_length=24, choices=STATUS_CHOICES, default=STATUS_QUEUED
    )

    branch = models.CharField(max_length=160, blank=True, default="")
    diff = models.TextField(blank=True, default="")

    VERDICT_PENDING = "pending"
    VERDICT_APPROVED = "approved"
    VERDICT_CHANGES = "changes_requested"
    VERDICT_CHOICES = (
        (VERDICT_PENDING, "pending"),
        (VERDICT_APPROVED, "approved"),
        (VERDICT_CHANGES, "changes_requested"),
    )
    review_verdict = models.CharField(
        max_length=20, choices=VERDICT_CHOICES, default=VERDICT_PENDING
    )

    test_passed = models.BooleanField(null=True, blank=True)
    test_summary = models.TextField(blank=True, default="")
    # How many code→review→test iterations the fix loop consumed.
    iterations = models.IntegerField(default=0)

    deploy_target = models.CharField(max_length=20, blank=True, default="")
    deploy_url = models.CharField(max_length=300, blank=True, default="")
    wiki_url = models.CharField(max_length=300, blank=True, default="")
    error = models.TextField(blank=True, default="")

    created_by = models.ForeignKey(
        "db.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ai_angela_runs_created",
    )
    # Human who approved the prod deploy in staging+gate mode.
    approved_by = models.ForeignKey(
        "db.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="ai_angela_runs_approved",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "ai_angela_run"
        verbose_name = "Angela run"
        verbose_name_plural = "Angela runs"
        indexes = [
            models.Index(
                fields=["workspace", "created_at"],
                name="ai_angela_ws_time_idx",
            ),
            models.Index(
                fields=["status"],
                name="ai_angela_status_idx",
            ),
            models.Index(
                fields=["issue_id"],
                name="ai_angela_issue_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"AngelaRun({self.id}, {self.status}, {self.deploy_mode})"


class AngelaStep(models.Model):
    """Append-only per-phase log line inside an :class:`AngelaRun`.

    Powers the live run feed in the Angela console. We never mutate a
    step after writing it — a phase that retries writes a NEW step with
    an incremented ``iteration``, mirroring the audit-trail discipline
    of AgentAction / AIAgentActionLog.
    """

    id = models.UUIDField(primary_key=True, default=uuid4, editable=False)
    run = models.ForeignKey(
        "ai.AngelaRun",
        on_delete=models.CASCADE,
        related_name="steps",
    )
    # Denormalised for workspace-scoped queries without joining the run.
    workspace_id = models.UUIDField(db_index=True)

    PHASE_PLAN = "plan"
    PHASE_CODE = "code"
    PHASE_REVIEW = "review"
    PHASE_TEST = "test"
    PHASE_DEPLOY = "deploy"
    PHASE_DOCS = "docs"
    PHASE_CHOICES = (
        (PHASE_PLAN, "plan"),
        (PHASE_CODE, "code"),
        (PHASE_REVIEW, "review"),
        (PHASE_TEST, "test"),
        (PHASE_DEPLOY, "deploy"),
        (PHASE_DOCS, "docs"),
    )
    phase = models.CharField(max_length=12, choices=PHASE_CHOICES)

    STATUS_STARTED = "started"
    STATUS_OK = "ok"
    STATUS_FAILED = "failed"
    STATUS_SKIPPED = "skipped"
    STATUS_CHOICES = (
        (STATUS_STARTED, "started"),
        (STATUS_OK, "ok"),
        (STATUS_FAILED, "failed"),
        (STATUS_SKIPPED, "skipped"),
    )
    status = models.CharField(max_length=12, choices=STATUS_CHOICES)

    title = models.CharField(max_length=200, blank=True, default="")
    # Truncated rationale / command output. Cap at ~8KB in the writer.
    detail = models.TextField(blank=True, default="")
    iteration = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "ai_angela_step"
        verbose_name = "Angela step"
        verbose_name_plural = "Angela steps"
        indexes = [
            models.Index(
                fields=["run", "created_at"],
                name="ai_angela_step_run_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"AngelaStep({self.phase}/{self.status})"
