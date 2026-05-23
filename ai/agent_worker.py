"""TZ 5.2 — Agent worker: bounded write on an issue's own project.

Three predicates make this safe to run **without a human in the loop**:

  1. **White-list of tools.** :data:`AGENT_TOOLS` is the *only* set of
     tool names we ever pass to Anthropic, so the model physically
     cannot ask us to call ``delete_issue`` / ``add_member`` / a
     workspace-admin endpoint. The Anthropic SDK rejects names not in
     its tool list, and our dispatcher in :func:`_apply_tool_use`
     re-checks the name on its way through — defence in depth.
  2. **Scope of the agent's own project.** Every write tool operates
     on the issue the worker was enqueued for. The dispatcher uses
     the issue's ``project_id`` as the only acceptable target —
     anything cross-project (a Label from another project, an
     assignee who isn't a member of this project) is logged and
     rejected before the Plane write runs.
  3. **Audit log per attempt.** Both applied and rejected actions
     land in :class:`ai.models.AIAgentActionLog`. Rejections are the
     more interesting half: they tell us *what the model tried*.

We deliberately leave ``delete_*``, sharing, role/membership changes,
and any cross-project write out of the white-list. There is no
"reject by name" list — that's a deny-list and would silently allow
new tools as they appear. The contract is "deny everything not on
:data:`AGENT_TOOLS`".

The worker also wraps Plane writes in :func:`ai.agent_triggers.
agent_acting` so the post_save signal that re-triggers the agent
recognises *its own* write and skips re-enqueueing. Without that, an
agent that sets a label would fire its own assignment trigger and
loop until the budget runs out.

This module intentionally does not depend on sprint-4 (TZ 4.2's
``run_command``) — at the time of writing 4.2 is unbuilt and the
worker must ship. When 4.2 lands, the internal agent loop here can
be replaced by a shared helper; the safety layer (apply + audit)
stays put.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from django.apps import apps as django_apps
from django.db import transaction

from ai import providers
from ai.agent_triggers import agent_acting
from ai.models import (
    AIAgent,
    AIAgentActionLog,
    AIUsageLog,
    WorkspaceAIConfig,
)
from ai.dedupe import (
    DEDUPE_LABEL_NAME,
    DEDUPE_SYSTEM,
    DEDUPE_TOOLS,
    already_deduped,
    build_dedupe_prompt,
    ensure_dedupe_label,
    find_candidates as find_dedupe_candidates,
)
from ai.describe import (
    DESCRIBE_SYSTEM,
    DESCRIBE_TOOLS,
    already_described,
    build_describe_prompt,
    should_describe,
)
from ai.search import build_context, retrieve
from ai.triage import (
    TRIAGE_SYSTEM,
    TRIAGE_TOOLS,
    _project_label_names,
    _project_member_emails,
    already_triaged,
    build_triage_prompt,
)
from ai.usage import record_usage, tokens_used_this_month


logger = logging.getLogger("plane.ai.agent_worker")


# The full set of names the model may emit. The dispatcher refuses
# anything outside this tuple — including future additions — until
# they are explicitly added here and given an apply_* handler.
AGENT_TOOLS: tuple[str, ...] = (
    "find_work_items",
    "set_labels",
    "set_priority",
    "suggest_assignee",
    "update_description",
    "add_comment",
)


# Hard cap on comment text the model may post. Comments aren't billed
# storage but a multi-kilobyte agent essay is a UX smell — readers
# will scroll past. Cap aligns with the dedup scenario's needs
# (a one-line "Possible duplicates: PROJ-42, PROJ-44") with ample
# slack for future scenarios.
MAX_COMMENT_CHARS = 2000


# Hard cap on the number of write actions per single worker run. The
# agent loop itself has its own step cap (``AGENT_MAX_STEPS``); this
# one limits how many Plane writes the issue can receive even if the
# loop fires many tool_use blocks in one step.
AGENT_MAX_ACTIONS = 5
AGENT_MAX_STEPS = 4

# Length cap on description rewrites. Plane stores
# ``description_stripped`` as TEXT — the field itself is unbounded —
# but we cap here to keep token costs predictable and to give the
# audit reviewer a length-of-blast-radius signal.
MAX_DESCRIPTION_CHARS = 4000


VALID_PRIORITIES = ("urgent", "high", "medium", "low", "none")


# ---------------------------------------------------------------------------
# Tool schemas passed to Anthropic
# ---------------------------------------------------------------------------
#
# Tool *inputs* never carry ``issue_id`` or ``project_id`` — those
# are pinned by the worker to the issue it was enqueued for. The
# model only chooses *what* to do, not *where*. This is the simplest
# way to make scope leaks impossible: the apply handler ignores
# anything the model tries to put in those fields.


AGENT_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "find_work_items",
        "description": (
            "Search the current project for related work items using "
            "semantic similarity. Returns at most 10 items."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Free-text query."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "set_labels",
        "description": (
            "Replace the current issue's labels with the supplied list "
            "of label names. Labels must already exist in this project."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Label names existing in this project.",
                },
            },
            "required": ["labels"],
        },
    },
    {
        "name": "set_priority",
        "description": (
            "Set the priority of the current issue. Allowed: "
            f"{', '.join(VALID_PRIORITIES)}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "priority": {"type": "string", "enum": list(VALID_PRIORITIES)},
            },
            "required": ["priority"],
        },
    },
    {
        "name": "suggest_assignee",
        "description": (
            "Assign a project member to the current issue. The user "
            "must already be a member of this project."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "user_email": {"type": "string"},
            },
            "required": ["user_email"],
        },
    },
    {
        "name": "update_description",
        "description": (
            "Overwrite the issue's plain-text description. "
            f"Capped at {MAX_DESCRIPTION_CHARS} characters."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "add_comment",
        "description": (
            "Post a comment on the current issue, authored by the "
            f"agent user. Capped at {MAX_COMMENT_CHARS} characters."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
            },
            "required": ["text"],
        },
    },
]


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def log_agent_action(
    *,
    agent: AIAgent,
    issue,
    tool_name: str,
    input_: dict,
    status: str,
    output: dict | None = None,
    error: str = "",
) -> AIAgentActionLog:
    """Append-only log row. Called once per tool_use attempt.

    ``input_`` carries the model's raw arguments — useful when a
    rejection happens and we want to see what the model intended.
    ``output`` describes what we did (e.g. ``{"labels_set": 2}``);
    on rejection it's empty.
    """
    return AIAgentActionLog.objects.create(
        agent=agent,
        workspace_id=issue.workspace_id,
        project_id=issue.project_id,
        issue_id=issue.id,
        tool_name=tool_name,
        input=input_ or {},
        output=output or {},
        status=status,
        error=error,
    )


# ---------------------------------------------------------------------------
# Tool dispatch (white-list + scope guard)
# ---------------------------------------------------------------------------


class _AgentRejection(Exception):
    """Raised by an apply_* handler when the requested action falls
    outside the agent's project scope or otherwise violates a safety
    invariant. Caught one level up, logged as ``rejected``, never
    propagated to Celery (a rejection is normal, not an error)."""


def _project_label_ids(project_id, names: list[str]) -> dict[str, Any]:
    """Resolve label names to ids, restricted to ``project_id``.

    Returns ``{name: id_or_None}`` so the caller can both apply the
    found labels AND log the names the model tried but that don't
    exist in this project (cross-project label injection attempt).
    """
    Label = django_apps.get_model("db", "Label")
    rows = list(
        Label.objects.filter(project_id=project_id, name__in=names).values(
            "id", "name"
        )
    )
    by_name = {r["name"]: r["id"] for r in rows}
    return {name: by_name.get(name) for name in names}


def _apply_find_work_items(*, issue, agent, params: dict, cfg) -> dict:
    """Read-only RAG lookup scoped to the issue's project. Read-only,
    but still scope-bound: we hand the retriever the *agent's user*
    so the workspace+ACL filter excludes anything the agent doesn't
    have read access to."""
    query = (params.get("query") or "").strip()
    if not query:
        raise _AgentRejection("empty query")
    chunks = retrieve(
        workspace_id=issue.workspace_id,
        user=agent.user,
        query=query,
        cfg=cfg,
        top_k=10,
    )
    # Project-scope the results: drop anything outside the agent's
    # own project. retrieve() already enforces ACL, but the agent's
    # white-list also forbids cross-project context — keeps the
    # model from suggesting moves that touch another project.
    same_project = [c for c in chunks if c.project_id == str(issue.project_id)]
    return {
        "count": len(same_project),
        "items": [
            {
                "source_type": c.source_type,
                "source_id": c.source_id,
                "snippet": c.content[:200],
            }
            for c in same_project
        ],
    }


def _apply_set_labels(*, issue, params: dict) -> dict:
    """Replace the current issue's labels with names that already
    exist in this project. Anything from another project is logged
    and skipped (rejection is per-name, not per-call: applying the
    valid subset is the right behaviour when the model mixes one bad
    name in with three good ones).

    Before applying the change we snapshot the *previous* label ids /
    names into the output. That snapshot is what the TZ 5.6 undo
    endpoint uses to restore the prior state — without it we'd have
    to guess what to roll back to (impossible: ``labels.set`` is
    destructive). Names alongside ids so the UI can display a
    human-readable preview ("вернуть метки: bug, frontend") without a
    second round-trip.
    """
    names = params.get("labels") or []
    if not isinstance(names, list):
        raise _AgentRejection("labels must be a list")
    if not names:
        raise _AgentRejection("empty labels list")
    resolved = _project_label_ids(issue.project_id, [str(n) for n in names])
    bad = [n for n, lid in resolved.items() if lid is None]
    if bad and not any(lid for lid in resolved.values()):
        raise _AgentRejection(
            f"no requested labels exist in project: {bad!r}"
        )
    Label = django_apps.get_model("db", "Label")
    # Snapshot BEFORE issue.labels.set() — the queryset is materialised
    # here, not lazy. Names alongside ids so the UI can render a
    # preview without joining back to db.Label.
    previous = list(
        issue.labels.values("id", "name").order_by("name")
    )
    ids = [lid for lid in resolved.values() if lid is not None]
    label_qs = Label.objects.filter(id__in=ids)
    issue.labels.set(label_qs)
    return {
        "labels_set": len(ids),
        "rejected_cross_project": bad,
        "previous_label_ids": [str(row["id"]) for row in previous],
        "previous_label_names": [row["name"] for row in previous],
    }


def _apply_set_priority(*, issue, params: dict) -> dict:
    priority = params.get("priority")
    if priority not in VALID_PRIORITIES:
        raise _AgentRejection(f"invalid priority {priority!r}")
    if issue.priority == priority:
        return {"priority": priority, "changed": False}
    issue.priority = priority
    issue.save(update_fields=["priority"])
    return {"priority": priority, "changed": True}


def _apply_suggest_assignee(*, issue, agent, params: dict) -> dict:
    """*Suggest* — not assign — a project member.

    TZ 5.3 invariant: "предложить ≠ назначить". A wrong
    auto-assignment pings the wrong human and quietly erodes trust;
    a comment is reversible. So this handler posts an
    ``IssueComment`` written by the agent, leaving the actual
    assignment decision to the team.

    Scope: same as before — the suggested user must be an active
    ``ProjectMember`` of this project. Cross-project / non-member /
    "agent suggests itself" all reject.

    A future workspace setting could opt into hard-assign mode (the
    TZ flags this as "если команда явно этого захочет"); not added
    here because no caller asks for it yet.
    """
    User = django_apps.get_model("db", "User")
    ProjectMember = django_apps.get_model("db", "ProjectMember")
    IssueComment = django_apps.get_model("db", "IssueComment")

    email = (params.get("user_email") or "").strip().lower()
    if not email:
        raise _AgentRejection("empty user_email")
    if email == (agent.user.email or "").lower():
        raise _AgentRejection("agent must not suggest itself")
    user = User.objects.filter(email__iexact=email).only("id", "email").first()
    if user is None:
        raise _AgentRejection(f"user {email!r} not found")
    is_member = ProjectMember.objects.filter(
        member_id=user.id,
        project_id=issue.project_id,
        is_active=True,
        deleted_at__isnull=True,
    ).exists()
    if not is_member:
        raise _AgentRejection(
            f"user {email!r} is not a member of project {issue.project_id}"
        )
    text = f"💡 Suggested assignee: {user.email}"
    comment = IssueComment.objects.create(
        project_id=issue.project_id,
        issue_id=issue.id,
        actor=agent.user,
        comment_stripped=text,
        comment_html=f"<p>{text}</p>",
        access="INTERNAL",
    )
    return {
        "suggested_user_id": str(user.id),
        "suggested_user_email": user.email,
        "comment_id": str(comment.id),
    }


def _apply_add_comment(*, issue, agent, params: dict) -> dict:
    """Post an IssueComment on the current issue, actor = the
    agent's user.

    Scope is implicit: the comment is bound to ``issue.project_id``
    and ``issue.id`` — the tool input never carries either field, so
    the model cannot redirect the comment elsewhere. Cap on text
    length protects against an agent essay that no human will read.
    """
    IssueComment = django_apps.get_model("db", "IssueComment")

    text = params.get("text")
    if not isinstance(text, str):
        raise _AgentRejection("text must be a string")
    text = text.strip()
    if not text:
        raise _AgentRejection("empty comment")
    if len(text) > MAX_COMMENT_CHARS:
        raise _AgentRejection(
            f"comment exceeds {MAX_COMMENT_CHARS} chars"
        )

    comment = IssueComment.objects.create(
        project_id=issue.project_id,
        issue_id=issue.id,
        actor=agent.user,
        comment_stripped=text,
        comment_html=f"<p>{text}</p>",
        access="INTERNAL",
    )
    return {
        "comment_id": str(comment.id),
        "comment_chars": len(text),
    }


def _apply_update_description(*, issue, params: dict) -> dict:
    text = params.get("text")
    if not isinstance(text, str):
        raise _AgentRejection("text must be a string")
    text = text.strip()
    if not text:
        raise _AgentRejection("empty description")
    if len(text) > MAX_DESCRIPTION_CHARS:
        raise _AgentRejection(
            f"description exceeds {MAX_DESCRIPTION_CHARS} chars"
        )
    issue.description_stripped = text
    issue.save(update_fields=["description_stripped"])
    return {"description_chars": len(text)}


_DISPATCH = {
    "find_work_items": _apply_find_work_items,
    "set_labels": _apply_set_labels,
    "set_priority": _apply_set_priority,
    "suggest_assignee": _apply_suggest_assignee,
    "update_description": _apply_update_description,
    "add_comment": _apply_add_comment,
}


def apply_agent_action(
    *,
    agent: AIAgent,
    issue,
    tool_name: str,
    tool_input: dict,
    cfg: WorkspaceAIConfig,
) -> AIAgentActionLog:
    """Single entry point for executing one tool_use block.

    Always returns the persisted ``AIAgentActionLog`` row so the
    caller can see what happened. Never raises for an
    out-of-scope/rejected tool call — those become ``status=rejected``
    rows. Handler exceptions become ``status=error`` rows. Both
    cases let the agent loop continue with the next tool_use.
    """
    if tool_name not in AGENT_TOOLS:
        # The Anthropic API shouldn't emit this — we never told the
        # model the tool existed — but a buggy SDK or a future model
        # quirk could. Belt-and-braces denial.
        return log_agent_action(
            agent=agent,
            issue=issue,
            tool_name=tool_name,
            input_=tool_input,
            status=AIAgentActionLog.STATUS_REJECTED,
            error=f"tool {tool_name!r} not in white-list",
        )

    handler = _DISPATCH[tool_name]

    try:
        if tool_name == "find_work_items":
            output = handler(issue=issue, agent=agent, params=tool_input, cfg=cfg)
        elif tool_name in ("suggest_assignee", "add_comment"):
            output = handler(issue=issue, agent=agent, params=tool_input)
        else:
            output = handler(issue=issue, params=tool_input)
    except _AgentRejection as exc:
        return log_agent_action(
            agent=agent,
            issue=issue,
            tool_name=tool_name,
            input_=tool_input,
            status=AIAgentActionLog.STATUS_REJECTED,
            error=str(exc),
        )
    except Exception as exc:  # noqa: BLE001 — we *must* swallow
        logger.exception(
            "agent action %s raised on issue=%s", tool_name, issue.id
        )
        return log_agent_action(
            agent=agent,
            issue=issue,
            tool_name=tool_name,
            input_=tool_input,
            status=AIAgentActionLog.STATUS_ERROR,
            error=f"{type(exc).__name__}: {exc}",
        )

    return log_agent_action(
        agent=agent,
        issue=issue,
        tool_name=tool_name,
        input_=tool_input,
        status=AIAgentActionLog.STATUS_APPLIED,
        output=output,
    )


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------
#
# Currently single-scenario (TZ 5.3 triage). Each future scenario
# (TZ 5.4 dedupe, 5.5 description rewrite) brings its own system
# prompt, build_*_prompt helper and tool subset, and plugs into the
# selection branch in :func:`run_agent_body`.


def _issue_for(issue_id):
    Issue = django_apps.get_model("db", "Issue")
    return (
        Issue.objects.filter(
            id=issue_id, deleted_at__isnull=True, is_draft=False
        )
        .select_related("workspace", "project")
        .first()
    )


def _cfg_for(workspace_id) -> WorkspaceAIConfig | None:
    return (
        WorkspaceAIConfig.objects.filter(
            workspace_id=workspace_id, enabled=True
        )
        .only("anthropic_key", "openai_key", "chat_model", "embed_model", "monthly_token_budget")
        .first()
    )


def _agent_for(issue) -> AIAgent | None:
    """The enabled AIAgent assigned to this issue, or None."""
    assignee_ids = list(issue.assignees.values_list("id", flat=True))
    if not assignee_ids:
        return None
    return (
        AIAgent.objects.filter(
            user_id__in=assignee_ids,
            workspace_id=issue.workspace_id,
            enabled=True,
        )
        .select_related("user")
        .first()
    )


def _run_scenario_loop(
    *,
    issue,
    agent,
    cfg,
    system_prompt: str,
    user_prompt: str,
    tool_names: tuple[str, ...],
    write_actions_already: int = 0,
) -> tuple[list[AIAgentActionLog], str | None, int]:
    """One scenario's Claude tool-use loop.

    Reused by every scenario in :func:`run_agent_body` so the safety
    machinery (token accounting, write-action cap, agent_acting,
    apply_agent_action) lives in exactly one place. Returns the
    action logs from this scenario, the final assistant text (if
    any), and the running write-action count for the *next* scenario
    to budget against.

    The caller picks the system prompt, the user prompt and the
    subset of :data:`AGENT_TOOL_SCHEMAS` to offer. The loop never
    invents a tool the caller didn't list — the white-list lives one
    level above this function (the worker's :data:`AGENT_TOOLS`).
    """
    tool_schemas = [
        s for s in AGENT_TOOL_SCHEMAS if s["name"] in tool_names
    ]
    chat = providers.ClaudeChat(api_key=cfg.anthropic_key)
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": user_prompt}
    ]
    action_logs: list[AIAgentActionLog] = []
    write_actions = write_actions_already
    final_text: str | None = None

    for _ in range(AGENT_MAX_STEPS):
        resp = chat.complete(
            system=system_prompt,
            messages=messages,
            tools=tool_schemas,
            model=cfg.chat_model or providers.CHAT_MODEL,
            max_tokens=1024,
            temperature=0.1,
        )
        record_usage(
            workspace_id=issue.workspace_id,
            user_id=agent.user_id,
            feature=AIUsageLog.FEATURE_AGENT,
            model=cfg.chat_model or providers.CHAT_MODEL,
            usage=resp.usage,
        )

        tool_uses = [
            b for b in resp.content if getattr(b, "type", None) == "tool_use"
        ]
        if not tool_uses:
            final_text = "".join(
                getattr(b, "text", "") for b in resp.content
                if getattr(b, "type", None) == "text"
            )
            break

        messages.append({"role": "assistant", "content": resp.content})
        results: list[dict[str, Any]] = []
        for tu in tool_uses:
            if write_actions >= AGENT_MAX_ACTIONS:
                # Budget guard for actions — surface the cap to the
                # model as a tool_result so it doesn't keep trying.
                results.append({
                    "type": "tool_result",
                    "tool_use_id": tu.id,
                    "content": json.dumps({"error": "action_cap_reached"}),
                    "is_error": True,
                })
                continue

            log = apply_agent_action(
                agent=agent,
                issue=issue,
                tool_name=tu.name,
                tool_input=dict(tu.input or {}),
                cfg=cfg,
            )
            action_logs.append(log)
            # find_work_items is read-only; everything else
            # consumes one slot of the action budget.
            if tu.name != "find_work_items":
                write_actions += 1

            if log.status == AIAgentActionLog.STATUS_APPLIED:
                body = json.dumps(log.output)
                is_error = False
            else:
                body = json.dumps({"error": log.error, "status": log.status})
                is_error = True
            results.append({
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": body,
                "is_error": is_error,
            })

        messages.append({"role": "user", "content": results})

    return action_logs, final_text, write_actions


def _run_triage_scenario(*, issue, agent, cfg, write_actions: int) -> tuple[list, str | None, int]:
    """Triage prompt (TZ 5.3) + scoped tool subset. Caller has
    already verified :func:`already_triaged` is False."""
    context_chunks = retrieve(
        workspace_id=issue.workspace_id,
        user=agent.user,
        query=f"{issue.name}\n\n{issue.description_stripped or ''}",
        cfg=cfg,
        top_k=8,
    )
    context_chunks = [
        c for c in context_chunks if c.project_id == str(issue.project_id)
    ]
    context = build_context(context_chunks)
    label_names = _project_label_names(issue.project_id)
    member_emails = _project_member_emails(
        issue.project_id, exclude_user_id=agent.user_id
    )
    user_prompt = build_triage_prompt(
        issue,
        context=context,
        label_names=label_names,
        member_emails=member_emails,
    )
    return _run_scenario_loop(
        issue=issue,
        agent=agent,
        cfg=cfg,
        system_prompt=TRIAGE_SYSTEM,
        user_prompt=user_prompt,
        tool_names=TRIAGE_TOOLS,
        write_actions_already=write_actions,
    )


def _run_dedupe_scenario(*, issue, agent, cfg, write_actions: int) -> tuple[list, str | None, int]:
    """Dedup judge-pass (TZ 5.4).

    Skipped when no candidate is above the cosine threshold —
    returns immediately with no Claude call, no audit rows. When
    candidates exist, we ensure the ``possible-duplicate`` label is
    provisioned in the project (so ``set_labels`` won't reject it
    later) and call Claude as a judge with only ``add_comment`` and
    ``set_labels`` on the menu.
    """
    candidates = find_dedupe_candidates(issue=issue, agent=agent, cfg=cfg)
    if not candidates:
        return [], None, write_actions

    # Provision the label before the LLM call. Failing this would
    # cause set_labels(["possible-duplicate"]) to be rejected by the
    # cross-project guard, which would falsely look like the model
    # invented the label — confusing to debug.
    ensure_dedupe_label(workspace=issue.workspace, project=issue.project)

    project_identifier = getattr(issue.project, "identifier", "") or ""
    user_prompt = build_dedupe_prompt(
        issue,
        candidates=candidates,
        project_identifier=project_identifier,
    )
    return _run_scenario_loop(
        issue=issue,
        agent=agent,
        cfg=cfg,
        system_prompt=DEDUPE_SYSTEM,
        user_prompt=user_prompt,
        tool_names=DEDUPE_TOOLS,
        write_actions_already=write_actions,
    )


def _run_describe_scenario(*, issue, agent, cfg, write_actions: int) -> tuple[list, str | None, int]:
    """Auto-description-draft scenario (TZ 5.5).

    Skipped when the description is already substantial — the
    :func:`should_describe` gate is checked by the caller, so reaching
    this function means the description was empty/one-line at the
    moment of the trigger fire. We assemble nearby project work via
    RAG, hand the title + context to Claude with ONLY ``add_comment``
    on the menu, and let the model emit a draft prefixed with
    :data:`ai.describe.DESCRIBE_MARKER`.

    Crucially the tool subset here EXCLUDES ``update_description`` —
    the user's existing content (even a one-liner) is left alone, the
    draft lands as a comment the human reviews. The marker prefix is
    what makes the draft idempotent on re-trigger
    (:func:`already_described`).
    """
    context_chunks = retrieve(
        workspace_id=issue.workspace_id,
        user=agent.user,
        query=issue.name,
        cfg=cfg,
        top_k=8,
    )
    # Project-scope the RAG result — the draft must be informed by
    # THIS project's prior work, not anything the agent's ACL might
    # otherwise let through (e.g. workspace-level pages).
    context_chunks = [
        c for c in context_chunks if c.project_id == str(issue.project_id)
    ]
    context = build_context(context_chunks)
    user_prompt = build_describe_prompt(issue, context=context)
    return _run_scenario_loop(
        issue=issue,
        agent=agent,
        cfg=cfg,
        system_prompt=DESCRIBE_SYSTEM,
        user_prompt=user_prompt,
        tool_names=DESCRIBE_TOOLS,
        write_actions_already=write_actions,
    )


def run_agent_body(issue_id) -> dict:
    """Main worker body. Called by ``ai.tasks.run_agent_on_workitem``.

    Runs the autonomous scenarios in sequence for one trigger fire:

      1. **Triage** (TZ 5.3) — first-time classification.
      2. **Dedupe** (TZ 5.4) — judge-pass over RAG candidates.
      3. **Describe** (TZ 5.5) — draft a description for issues
         created with title only. Order matters: dedupe must check
         its own gate BEFORE describe writes a comment, otherwise
         dedupe's any-applied-comment heuristic would mis-fire.

    Each scenario has its own ``already_*`` idempotency gate so a
    re-trigger (human edit) does not re-run a scenario whose
    durable side-effect already landed. Returns a summary dict —
    not used by Celery but invaluable for tests and ad-hoc runs.
    """
    issue = _issue_for(issue_id)
    if issue is None:
        logger.info("agent_worker: issue %s gone/draft/deleted", issue_id)
        return {"status": "skipped", "reason": "issue_missing"}

    cfg = _cfg_for(issue.workspace_id)
    if cfg is None or not cfg.anthropic_key:
        logger.info("agent_worker: ws %s has no AI config", issue.workspace_id)
        return {"status": "skipped", "reason": "no_config"}

    # Budget gate. Mirrors the view-layer ``require_ai_budget``
    # decorator — but this code path bypasses HTTP, so we re-check.
    used = tokens_used_this_month(issue.workspace_id)
    if used >= cfg.monthly_token_budget:
        logger.warning(
            "agent_worker: budget exhausted for ws %s (used=%d budget=%d)",
            issue.workspace_id,
            used,
            cfg.monthly_token_budget,
        )
        return {"status": "skipped", "reason": "budget_exhausted"}

    agent = _agent_for(issue)
    if agent is None:
        # Trigger fired on the ``ai-agent`` label alone (no
        # assignee). No concrete agent row → no user to write
        # records under.
        logger.info("agent_worker: no enabled AIAgent for issue %s", issue.id)
        return {"status": "skipped", "reason": "no_agent"}

    scenarios_run: list[str] = []
    all_actions: list[AIAgentActionLog] = []
    final_text: str | None = None
    write_actions = 0

    # `agent_acting` spans ALL scenarios — one trigger fire = one
    # held flag — so no scenario's writes accidentally enqueue
    # another agent run.
    with agent_acting(issue.id):
        if not already_triaged(issue.id):
            logs, text, write_actions = _run_triage_scenario(
                issue=issue, agent=agent, cfg=cfg, write_actions=write_actions
            )
            scenarios_run.append("triage")
            all_actions.extend(logs)
            final_text = text

        if not already_deduped(issue.id):
            logs, text, write_actions = _run_dedupe_scenario(
                issue=issue, agent=agent, cfg=cfg, write_actions=write_actions
            )
            if logs or text:
                scenarios_run.append("dedupe")
                all_actions.extend(logs)
                final_text = text or final_text

        if should_describe(issue) and not already_described(issue.id):
            logs, text, write_actions = _run_describe_scenario(
                issue=issue, agent=agent, cfg=cfg, write_actions=write_actions
            )
            scenarios_run.append("describe")
            all_actions.extend(logs)
            final_text = text or final_text

    if not scenarios_run:
        # Every scenario skipped because its gate (idempotency or
        # trigger predicate) was closed. Reported as a deliberate
        # skip rather than a silent drop.
        return {
            "status": "skipped",
            "reason": "all_scenarios_idempotent",
            "issue_id": str(issue.id),
        }

    return {
        "status": "ok",
        "issue_id": str(issue.id),
        "scenarios": scenarios_run,
        "actions": len(all_actions),
        "applied": sum(
            1 for a in all_actions if a.status == AIAgentActionLog.STATUS_APPLIED
        ),
        "rejected": sum(
            1 for a in all_actions if a.status == AIAgentActionLog.STATUS_REJECTED
        ),
        "errored": sum(
            1 for a in all_actions if a.status == AIAgentActionLog.STATUS_ERROR
        ),
        "final_text": final_text,
    }


__all__ = [
    "AGENT_TOOLS",
    "AGENT_TOOL_SCHEMAS",
    "AGENT_MAX_ACTIONS",
    "AGENT_MAX_STEPS",
    "MAX_DESCRIPTION_CHARS",
    "apply_agent_action",
    "log_agent_action",
    "run_agent_body",
]
