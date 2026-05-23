"""TZ 5.5 — auto-description draft scenario invariants.

Locks in the DoD bullets that don't need a live Anthropic call:

  - the trigger gate (``should_describe``) fires only on empty / very
    short descriptions; a substantial description leaves the issue
    alone;
  - the idempotency gate (``already_described``) flips after the
    marker-prefixed comment lands, so a re-trigger is a no-op;
  - the scenario tool subset is ``("add_comment",)`` ONLY —
    ``update_description`` is intentionally absent so the model
    cannot silently rewrite user content;
  - the marker prefix is preserved verbatim in the prompt so the
    model is told what to emit (and the idempotency lookup can find
    it later);
  - all actions land in the audit log (parent worker invariant —
    here we verify the describe-shaped row is shaped correctly).

The end-to-end Claude loop with a mocked client is exercised in
TZ 5.7. Here we drive the apply layer / scenario helpers directly.
"""

from __future__ import annotations

import pytest

from ai.agent_worker import apply_agent_action
from ai.describe import (
    DESCRIBE_MARKER,
    DESCRIBE_MIN_DESCRIPTION_CHARS,
    DESCRIBE_TOOLS,
    already_described,
    build_describe_prompt,
    should_describe,
)
from ai.models import AIAgentActionLog


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def describe_setup(
    db,
    make_user,
    make_workspace,
    make_workspace_member,
    make_project,
    make_ai_config,
    make_issue,
):
    """Workspace with an agent, a project the agent is a member of,
    and a freshly created issue (title only — the trigger case)."""
    from plane.db.models import ProjectMember

    from ai.models import AIAgent

    owner = make_user("owner")
    ws = make_workspace(owner=owner)
    cfg = make_ai_config(ws)
    project = make_project(workspace=ws, created_by=owner)

    agent_user = make_user("agent")
    make_workspace_member(workspace=ws, user=agent_user, role=15)
    ProjectMember.objects.create(
        workspace=ws, project=project, member=agent_user, role=15, is_active=True
    )
    agent = AIAgent.objects.create(user=agent_user, workspace=ws, enabled=True)

    issue = make_issue(
        workspace=ws,
        project=project,
        name="Add dark mode toggle to settings page",
        description="",
    )
    # Assign the agent user — _agent_for(issue) resolves via the
    # assignees relation, so without this run_agent_body would
    # return reason="no_agent" before the describe scenario runs.
    issue.assignees.add(
        agent_user,
        through_defaults={"workspace_id": ws.id, "project_id": project.id},
    )

    class _Setup:
        pass

    s = _Setup()
    s.owner = owner
    s.workspace = ws
    s.project = project
    s.cfg = cfg
    s.agent_user = agent_user
    s.agent = agent
    s.issue = issue
    return s


# ---------------------------------------------------------------------------
# Tool subset: no update_description on the menu
# ---------------------------------------------------------------------------


def test_describe_tool_subset_excludes_update_description():
    """The TZ 5.5 hard invariant: never silently overwrite the user's
    description. The scenario's only write tool is ``add_comment``."""
    assert DESCRIBE_TOOLS == ("add_comment",)
    for forbidden in ("update_description", "set_priority", "set_labels",
                       "suggest_assignee", "delete_issue"):
        assert forbidden not in DESCRIBE_TOOLS


# ---------------------------------------------------------------------------
# Trigger gate: should_describe
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_should_describe_true_on_empty_description(describe_setup):
    """The headline case: an issue created with title only triggers
    a draft."""
    assert describe_setup.issue.description_stripped in ("", None)
    assert should_describe(describe_setup.issue) is True


@pytest.mark.django_db
def test_should_describe_true_on_whitespace_only(describe_setup):
    """A description that's just spaces / newlines is empty for our
    purposes — :func:`should_describe` strips before measuring."""
    # Plane recomputes description_stripped from description_html on
    # any save of an existing Issue (see Issue.save in
    # apps/api/plane/db/models/issue.py). So we set the HTML; Plane
    # strips it back into stripped on save.
    describe_setup.issue.description_html = "   \n\t  "
    describe_setup.issue.save(update_fields=["description_html", "description_stripped"])
    assert should_describe(describe_setup.issue) is True


@pytest.mark.django_db
def test_should_describe_true_just_below_threshold(describe_setup):
    """A description one char short of the threshold still triggers —
    the inequality is strict (``<``)."""
    describe_setup.issue.description_html = "<p>" + ("x" * (DESCRIBE_MIN_DESCRIPTION_CHARS - 1)) + "</p>"
    describe_setup.issue.save(update_fields=["description_html", "description_stripped"])
    assert should_describe(describe_setup.issue) is True


@pytest.mark.django_db
def test_should_describe_false_at_threshold(describe_setup):
    """Exactly threshold-length is "enough" — we don't fire."""
    describe_setup.issue.description_html = "<p>" + ("x" * DESCRIBE_MIN_DESCRIPTION_CHARS) + "</p>"
    describe_setup.issue.save(update_fields=["description_html", "description_stripped"])
    assert should_describe(describe_setup.issue) is False


@pytest.mark.django_db
def test_should_describe_false_on_substantial_description(describe_setup):
    """The TZ 5.5 invariant: a human-written description is left alone.
    A real description (~200 chars) blocks the trigger."""
    real = (
        "Need to add a toggle in the user settings page that switches "
        "the whole app between light and dark themes. Should persist "
        "across reloads and respect the system preference by default."
    )
    describe_setup.issue.description_html = "<p>" + real + "</p>"
    describe_setup.issue.save(update_fields=["description_html", "description_stripped"])
    assert should_describe(describe_setup.issue) is False


# ---------------------------------------------------------------------------
# Idempotency gate: already_described
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_already_described_false_on_fresh_issue(describe_setup):
    assert already_described(describe_setup.issue.id) is False


@pytest.mark.django_db
def test_already_described_flips_after_marker_comment(describe_setup):
    """A successful draft post must close the gate so a re-trigger
    doesn't produce a second draft.

    We apply the action through the worker's dispatcher so the audit
    row is exactly what production would write."""
    draft_text = (
        f"{DESCRIBE_MARKER}\n\n"
        "Контекст: пользователь просит тёмную тему.\n"
        "Шаги: добавить toggle, сохранить выбор, ...\n"
        "Критерии готовности: ..."
    )
    log = apply_agent_action(
        agent=describe_setup.agent,
        issue=describe_setup.issue,
        tool_name="add_comment",
        tool_input={"text": draft_text},
        cfg=describe_setup.cfg,
    )
    assert log.status == AIAgentActionLog.STATUS_APPLIED
    assert already_described(describe_setup.issue.id) is True


@pytest.mark.django_db
def test_already_described_ignores_non_marker_comments(describe_setup):
    """A normal (non-draft) comment — for example the dedupe scenario's
    "Possible duplicates: PROJ-42" — must NOT flip the describe gate.
    The marker prefix is the contract."""
    log = apply_agent_action(
        agent=describe_setup.agent,
        issue=describe_setup.issue,
        tool_name="add_comment",
        tool_input={"text": "Possible duplicates: PROJ-1"},
        cfg=describe_setup.cfg,
    )
    assert log.status == AIAgentActionLog.STATUS_APPLIED
    assert already_described(describe_setup.issue.id) is False


@pytest.mark.django_db
def test_already_described_ignores_rejected_marker_comments(describe_setup):
    """A *rejected* attempt (e.g. an empty comment that happened to
    have the marker as a prefix of nothing) must not flip the gate —
    nothing visible actually landed on the issue."""
    AIAgentActionLog.objects.create(
        agent=describe_setup.agent,
        workspace_id=describe_setup.workspace.id,
        project_id=describe_setup.project.id,
        issue_id=describe_setup.issue.id,
        tool_name="add_comment",
        input={"text": f"{DESCRIBE_MARKER}\n\n..."},
        output={},
        status=AIAgentActionLog.STATUS_REJECTED,
        error="something broke",
    )
    assert already_described(describe_setup.issue.id) is False


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_build_describe_prompt_carries_title_and_marker(describe_setup):
    """The user message must contain the issue title (so the model
    has something to describe) and tell the model the exact marker
    string to emit — that's what makes the comment recognisable."""
    prompt = build_describe_prompt(describe_setup.issue, context="")
    assert describe_setup.issue.name in prompt
    assert str(describe_setup.issue.id) in prompt
    assert DESCRIBE_MARKER in prompt
    # Empty context yields the "nothing found" hint, not a silent gap.
    assert "не найдено" in prompt


@pytest.mark.django_db
def test_build_describe_prompt_includes_context_when_present(describe_setup):
    """When RAG produced context chunks, the prompt includes them in
    a labelled block so the model can lean on prior project work."""
    context = "[work_item:abc] Similar request about theming"
    prompt = build_describe_prompt(describe_setup.issue, context=context)
    assert "[work_item:abc]" in prompt
    # And the marker is still on the menu.
    assert DESCRIBE_MARKER in prompt


@pytest.mark.django_db
def test_build_describe_prompt_shows_existing_short_description(describe_setup):
    """A title-plus-one-liner case: the existing short text is shown
    to the model so the draft expands on it rather than ignoring it."""
    describe_setup.issue.description_html = "<p>TLDR: toggle theme</p>"
    describe_setup.issue.save(update_fields=["description_html", "description_stripped"])
    prompt = build_describe_prompt(describe_setup.issue, context="")
    assert "TLDR: toggle theme" in prompt


# ---------------------------------------------------------------------------
# Apply layer: marker-prefixed comment lands and audits correctly
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_draft_comment_lands_with_marker_and_is_audited(describe_setup):
    """End-of-pipeline check: when the model emits an add_comment
    starting with the marker, the comment is created on the issue
    by the agent user AND the audit row carries the marker so a
    later ``already_described`` lookup succeeds."""
    from plane.db.models import IssueComment

    draft_text = f"{DESCRIBE_MARKER}\n\nКонтекст: пример.\nШаги: 1, 2.\nГотово: ..."
    log = apply_agent_action(
        agent=describe_setup.agent,
        issue=describe_setup.issue,
        tool_name="add_comment",
        tool_input={"text": draft_text},
        cfg=describe_setup.cfg,
    )
    assert log.status == AIAgentActionLog.STATUS_APPLIED
    # The comment itself is on this issue, this project, by this agent.
    comment = IssueComment.objects.get(id=log.output["comment_id"])
    assert comment.issue_id == describe_setup.issue.id
    assert comment.project_id == describe_setup.project.id
    assert comment.actor_id == describe_setup.agent_user.id
    assert comment.comment_stripped.startswith(DESCRIBE_MARKER)
    # The issue's description was NOT touched — that's the TZ 5.5
    # invariant the whole scenario exists to defend.
    describe_setup.issue.refresh_from_db()
    assert (describe_setup.issue.description_stripped or "") == ""
    # Audit row carries the marker so already_described finds it.
    persisted = AIAgentActionLog.objects.get(id=log.id)
    assert persisted.input["text"].startswith(DESCRIBE_MARKER)
    assert persisted.tool_name == "add_comment"


# ---------------------------------------------------------------------------
# Integration: run_agent_body picks describe up
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_run_agent_body_runs_describe_only_when_description_short(
    describe_setup, monkeypatch
):
    """``run_agent_body`` plumbs the describe scenario through. We
    monkey-patch the scenario function to a sentinel so we can detect
    whether the worker would have called Claude for it, without
    touching the network.

    Case A: short description → describe scenario invoked.
    Case B: substantial description → describe scenario NOT invoked.
    """
    from ai import agent_worker

    # Close triage and dedupe gates so the worker doesn't try to
    # exercise their Claude calls during this test.
    for tool in ("set_priority", "add_comment"):
        AIAgentActionLog.objects.create(
            agent=describe_setup.agent,
            workspace_id=describe_setup.workspace.id,
            project_id=describe_setup.project.id,
            issue_id=describe_setup.issue.id,
            tool_name=tool,
            input={"priority": "low"} if tool == "set_priority" else {"text": "x"},
            output={"priority": "low"} if tool == "set_priority" else {"comment_id": "x"},
            status=AIAgentActionLog.STATUS_APPLIED,
        )

    calls: list[str] = []

    def _fake_run_describe(*, issue, agent, cfg, write_actions):
        calls.append(str(issue.id))
        return [], None, write_actions

    monkeypatch.setattr(agent_worker, "_run_describe_scenario", _fake_run_describe)
    # Belt: if the Claude path is somehow reached, fail loudly.
    monkeypatch.setattr(
        agent_worker.providers,
        "ClaudeChat",
        lambda **kw: pytest.fail("worker reached Claude unexpectedly"),
    )

    # Case A: empty description, scenario fires.
    result = agent_worker.run_agent_body(describe_setup.issue.id)
    assert calls == [str(describe_setup.issue.id)]
    assert "describe" in result["scenarios"]

    # Case B: substantial description, gate closes, scenario skipped.
    # Plane recomputes description_stripped from description_html on
    # every save — so the test sets the html, which strips to the
    # same text we'd want stripped to be.
    calls.clear()
    real = (
        "This is a real, human-written description that is well above "
        "the trigger threshold and should leave the agent alone."
    )
    describe_setup.issue.description_html = "<p>" + real + "</p>"
    describe_setup.issue.save(update_fields=["description_html", "description_stripped"])

    result = agent_worker.run_agent_body(describe_setup.issue.id)
    assert calls == []
    assert result["status"] == "skipped"
    assert result["reason"] == "all_scenarios_idempotent"
