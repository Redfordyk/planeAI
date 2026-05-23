"""TZ 5.7 — agent safety invariants (blocking PR suite).

This module is the final gate before TZ 5.8 acceptance. The test
names mirror the TZ 5.7 DoD bullets exactly so a reviewer can map
ТЗ → tests at a glance:

  - test_agent_cannot_delete            (1.1)
  - test_agent_whitelist_only           (1.2)
  - test_agent_stays_in_project         (2.1)
  - test_agent_no_cross_workspace       (2.2)
  - test_agent_no_self_trigger          (3.1)
  - test_agent_action_limit             (3.2)
  - test_every_action_logged            (4)
  - test_triage_does_not_hard_assign    (5.a)
  - test_dedupe_does_not_close          (5.b)
  - test_describe_does_not_overwrite    (5.c)

The end-to-end Claude loop is exercised via a scripted fake (see
:class:`_FakeChat` below) — no live API calls. The fake is the only
way to test the agent loop's *behaviour under hostile model output*
("model tries delete", "model returns 10 tool_uses in a row"); for
those tests, calling :func:`apply_agent_action` directly would let
the agent loop's own guards (write-action cap, agent_acting wrap)
slip past coverage.

Where a TZ bullet was already covered by an earlier sprint's test
file (e.g. ``test_agent_worker.py`` for white-list rejection at the
dispatcher), we re-assert the invariant here from the loop-level
entry point — that way a refactor that breaks the contract at any
layer (dispatcher, loop, schemas) fails this file's tests.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

from ai import agent_worker, providers
from ai.agent_worker import (
    AGENT_MAX_ACTIONS,
    AGENT_TOOLS,
    apply_agent_action,
    run_agent_body,
)
from ai.models import AIAgent, AIAgentActionLog


# ---------------------------------------------------------------------------
# Fake Anthropic — minimal duck-typed shape ClaudeChat returns
# ---------------------------------------------------------------------------


@dataclass
class _ToolUse:
    """Mirrors ``anthropic.types.ToolUseBlock``. Only the four attrs
    the worker reads (``type``, ``id``, ``name``, ``input``)."""

    name: str
    input: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: f"toolu_{uuid.uuid4().hex[:12]}")
    type: str = "tool_use"


@dataclass
class _TextBlock:
    """Mirrors ``anthropic.types.TextBlock`` — ``type`` + ``text``."""

    text: str = ""
    type: str = "text"


@dataclass
class _Response:
    """Mirrors the ``Message`` object the agent worker iterates over."""

    content: list[Any]
    usage: dict[str, int] = field(default_factory=lambda: {
        "input_tokens": 100,
        "output_tokens": 20,
    })


class _FakeChat:
    """Scripted stand-in for :class:`providers.ClaudeChat`.

    Each ``complete()`` call pops one entry off ``script``. Each entry
    is a list of blocks (``_ToolUse`` / ``_TextBlock``) that becomes
    the response's ``content``. After the script is exhausted, every
    further call returns a single text block — that's how the agent
    loop terminates without an infinite mock.

    The constructor signature matches ``ClaudeChat`` so the worker
    can pass ``api_key=...`` without us caring.
    """

    def __init__(self, api_key: str | None = None, **_) -> None:
        self.calls: list[dict[str, Any]] = []
        self.script: list[list[Any]] = []

    def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str = "",
        max_tokens: int = 1024,
        temperature: float = 0.1,
    ) -> _Response:
        # Record the call so tests can inspect the offered tool set.
        self.calls.append(
            {
                "system": system,
                "messages": messages,
                "tools": [t["name"] for t in (tools or [])],
                "model": model,
            }
        )
        if not self.script:
            # Defensive default — ends the loop quietly. Without this
            # a misconfigured test would spin forever inside the
            # AGENT_MAX_STEPS bound (still finite, but confusing).
            return _Response(content=[_TextBlock(text="ok")])
        next_blocks = self.script.pop(0)
        return _Response(content=next_blocks)


@pytest.fixture
def fake_chat(monkeypatch):
    """Install ``_FakeChat`` as ``providers.ClaudeChat`` so the worker
    picks it up. Returns the (singleton-ish) holder so each test can
    set ``holder.fake.script = [...]`` before driving the worker.

    The factory closure is the trick: ``providers.ClaudeChat(...)`` is
    called by the worker once per scenario (sometimes more) — we
    return the SAME fake each time so the test only needs to populate
    one script, regardless of how many scenarios fire.
    """

    class _Holder:
        fake = _FakeChat()

    monkeypatch.setattr(providers, "ClaudeChat", lambda **kw: _Holder.fake)
    return _Holder


@pytest.fixture
def mock_retrieve(monkeypatch):
    """Replace :func:`ai.search.retrieve` (and its in-module copies)
    with a no-op that returns ``[]``. The safety tests don't care
    about RAG content — they care about what the agent loop does
    with the model's output. Real ``retrieve`` would otherwise need
    a working OpenAI mock + DocumentChunk rows."""
    from ai import agent_worker as worker_mod
    from ai import dedupe as dedupe_mod
    from ai import describe as describe_mod  # noqa: F401 (imported for parity)

    monkeypatch.setattr(worker_mod, "retrieve", lambda **kw: [])
    monkeypatch.setattr(dedupe_mod, "retrieve", lambda **kw: [])


@pytest.fixture
def safety_setup(
    db,
    make_user,
    make_workspace,
    make_workspace_member,
    make_project,
    make_ai_config,
    make_issue,
):
    """Reusable scaffold: workspace + project + agent + ONE issue
    the agent is assigned to.

    The issue has a substantial description so the describe scenario
    naturally skips — keeps the test focused on whichever scenario
    each test populates the script for.
    """
    from plane.db.models import ProjectMember

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
        name="safety-test issue",
        # Long enough that describe.should_describe() returns False;
        # individual tests that want describe to fire override this.
        description=(
            "This issue carries a real description, well above the "
            "describe threshold, so the describe scenario is gated out "
            "for tests that focus on triage or dedupe."
        ),
    )
    # M2M through `db.IssueAssignee` extends `ProjectBaseModel` which
    # requires workspace_id + project_id NOT NULL — `.add()` alone
    # would leave those null and crash on insert.
    issue.assignees.add(
        agent_user,
        through_defaults={"workspace_id": ws.id, "project_id": project.id},
    )

    class _S:
        pass

    s = _S()
    s.owner = owner
    s.workspace = ws
    s.cfg = cfg
    s.project = project
    s.agent_user = agent_user
    s.agent = agent
    s.issue = issue
    return s


def _close_other_scenario_gates(setup, *, triage=False, dedupe=False, describe=False):
    """Seed audit rows so the *other* scenarios skip and the test
    targets exactly one. Equivalent to "fast-forward through scenarios
    we're not testing"."""
    if triage:
        AIAgentActionLog.objects.create(
            agent=setup.agent,
            workspace_id=setup.workspace.id,
            project_id=setup.project.id,
            issue_id=setup.issue.id,
            tool_name="set_priority",
            input={"priority": "low"},
            output={"priority": "low"},
            status=AIAgentActionLog.STATUS_APPLIED,
        )
    if dedupe:
        AIAgentActionLog.objects.create(
            agent=setup.agent,
            workspace_id=setup.workspace.id,
            project_id=setup.project.id,
            issue_id=setup.issue.id,
            tool_name="add_comment",
            input={"text": "Possible duplicates: PROJ-1"},
            output={"comment_id": "deadbeef", "comment_chars": 30},
            status=AIAgentActionLog.STATUS_APPLIED,
        )
    if describe:
        from ai.describe import DESCRIBE_MARKER

        AIAgentActionLog.objects.create(
            agent=setup.agent,
            workspace_id=setup.workspace.id,
            project_id=setup.project.id,
            issue_id=setup.issue.id,
            tool_name="add_comment",
            input={"text": f"{DESCRIBE_MARKER}\n\nseeded"},
            output={"comment_id": "feedface", "comment_chars": 50},
            status=AIAgentActionLog.STATUS_APPLIED,
        )


# ===========================================================================
# 1. Restrictions on the tool set
# ===========================================================================


@pytest.mark.django_db
def test_agent_cannot_delete(safety_setup, fake_chat, mock_retrieve):
    """The model emits a ``delete_issue`` tool_use. The dispatcher MUST
    refuse with an explicit "not in white-list" rejection — never
    invoke a Django delete path.

    Defence in depth: ``delete_issue`` is also absent from
    :data:`AGENT_TOOL_SCHEMAS`, so Anthropic's SDK would itself
    refuse the name. But a buggy SDK or a future model quirk could
    bypass that, so :func:`apply_agent_action` checks again.
    """
    log = apply_agent_action(
        agent=safety_setup.agent,
        issue=safety_setup.issue,
        tool_name="delete_issue",
        tool_input={"issue_id": str(safety_setup.issue.id)},
        cfg=safety_setup.cfg,
    )
    assert log.status == AIAgentActionLog.STATUS_REJECTED
    assert "white-list" in log.error
    # The issue is still there — no delete leaked through.
    safety_setup.issue.refresh_from_db()
    assert safety_setup.issue.deleted_at is None


@pytest.mark.django_db
def test_agent_whitelist_only(safety_setup):
    """Any tool name outside :data:`AGENT_TOOLS` is rejected by the
    dispatcher. We loop a small corpus of plausibly-named "trojan"
    tools — close to real names but not on the list — so a future
    accidental rename in ``AGENT_TOOLS`` (e.g. plural→singular) is
    detected as a regression here, not in prod.
    """
    trojan_names = [
        "close_issue",
        "merge_issue",
        "add_member",
        "remove_member",
        "set_workspace_admin",
        "set_label",  # singular — the real one is set_labels
        "set_labels_globally",
    ]
    for name in trojan_names:
        assert name not in AGENT_TOOLS, f"trojan name {name!r} leaked into white-list"
        log = apply_agent_action(
            agent=safety_setup.agent,
            issue=safety_setup.issue,
            tool_name=name,
            tool_input={},
            cfg=safety_setup.cfg,
        )
        assert log.status == AIAgentActionLog.STATUS_REJECTED, f"{name} not rejected"


# ===========================================================================
# 2. Project / workspace scope
# ===========================================================================


@pytest.mark.django_db
def test_agent_stays_in_project(safety_setup, make_project):
    """The model names a label that exists ONLY in a sibling project
    in the SAME workspace. The dispatcher must refuse — agent writes
    are bound to the issue's own project.

    We assert two things:
      - the rejection is logged (per TZ 5.2 audit invariant);
      - the issue's labels list stays empty (no partial application).
    """
    from plane.db.models import Label

    foreign_project = make_project(
        workspace=safety_setup.workspace, created_by=safety_setup.owner
    )
    foreign_label = Label.objects.create(
        workspace=safety_setup.workspace,
        project=foreign_project,
        name="urgent-from-elsewhere",
        color="#000",
    )

    log = apply_agent_action(
        agent=safety_setup.agent,
        issue=safety_setup.issue,
        tool_name="set_labels",
        tool_input={"labels": [foreign_label.name]},
        cfg=safety_setup.cfg,
    )
    assert log.status == AIAgentActionLog.STATUS_REJECTED
    assert safety_setup.issue.labels.count() == 0


@pytest.mark.django_db
def test_agent_no_cross_workspace(
    safety_setup, make_workspace, make_project, make_user, make_workspace_member
):
    """Two-workspace scenario: a Label with the same name exists in a
    DIFFERENT workspace's project. The agent tries ``set_labels`` with
    that name. Even though the name is real *somewhere*, the
    project-scoped resolver only looks in ``issue.project_id``, which
    lives in workspace A. The label from workspace B is invisible →
    rejected.

    Same shape for ``suggest_assignee``: a user who is a member of
    workspace B (but not of any project in workspace A) must not be
    suggestable.
    """
    from plane.db.models import Label, ProjectMember

    # Workspace B with its own project and a label of the same name.
    other_ws = make_workspace()
    other_project = make_project(workspace=other_ws, created_by=other_ws.owner)
    Label.objects.create(
        workspace=other_ws,
        project=other_project,
        name="bug",  # same name we'll try in safety_setup
        color="#f00",
    )

    log = apply_agent_action(
        agent=safety_setup.agent,
        issue=safety_setup.issue,
        tool_name="set_labels",
        tool_input={"labels": ["bug"]},
        cfg=safety_setup.cfg,
    )
    assert log.status == AIAgentActionLog.STATUS_REJECTED
    # No leakage: the "bug" label from the other workspace must not
    # land on our issue.
    assert safety_setup.issue.labels.count() == 0

    # And the assignee case — a user from workspace B with no
    # membership in safety_setup.project must not be suggestable.
    stranger = make_user("ws-b-stranger")
    make_workspace_member(workspace=other_ws, user=stranger, role=15)
    ProjectMember.objects.create(
        workspace=other_ws, project=other_project, member=stranger, role=15, is_active=True
    )
    log = apply_agent_action(
        agent=safety_setup.agent,
        issue=safety_setup.issue,
        tool_name="suggest_assignee",
        tool_input={"user_email": stranger.email},
        cfg=safety_setup.cfg,
    )
    assert log.status == AIAgentActionLog.STATUS_REJECTED
    assert "not a member" in log.error


# ===========================================================================
# 3. Self-trigger loop & action limit
# ===========================================================================


@pytest.mark.django_db
def test_agent_no_self_trigger(safety_setup, monkeypatch):
    """The post_save signal MUST recognise the agent's own write and
    skip re-enqueueing.

    Why this matters: the agent calls ``issue.save()`` for
    ``set_priority`` and ``update_description``, and ``issue.labels.set``
    triggers M2M signals. Without the ``agent_acting`` Redis flag,
    each of those saves would re-fire the post_save → enqueue a
    fresh agent run → tight loop that burns the token budget in
    minutes (the TZ flags this as "способна за минуты сжечь весь
    бюджет"). We verify the guard at the signal level: while
    ``agent_acting`` is held, ``on_issue_saved_for_agent`` returns
    without calling :func:`_enqueue_agent`.
    """
    from ai import agent_triggers

    enqueues: list = []
    monkeypatch.setattr(
        agent_triggers, "_enqueue_agent", lambda issue_id: enqueues.append(issue_id)
    )

    with agent_triggers.agent_acting(safety_setup.issue.id):
        # While the flag is held, even a "save" event must NOT enqueue.
        agent_triggers.on_issue_saved_for_agent(
            sender=type(safety_setup.issue), instance=safety_setup.issue
        )
    assert enqueues == [], "agent retriggered itself while agent_acting held"

    # Sanity check: with the flag released, an issue that *would*
    # normally trigger (assigned to an enabled agent) DOES enqueue.
    # We don't run this for the assertion alone — instead we assert
    # is_agent_acting() flips back to False so the next genuine save
    # is not silently suppressed.
    assert agent_triggers.is_agent_acting(safety_setup.issue.id) is False


@pytest.mark.django_db
def test_agent_action_limit(safety_setup, fake_chat, mock_retrieve, monkeypatch):
    """The loop applies at most :data:`AGENT_MAX_ACTIONS` write actions
    per worker run. After that, further tool_use blocks come back to
    the model as ``action_cap_reached`` error tool_results and never
    reach the apply layer.

    We force the loop into triage (close dedupe/describe gates) and
    script a single turn that emits MANY ``set_priority`` calls.
    Then we assert exactly :data:`AGENT_MAX_ACTIONS` audit rows
    landed — the surplus calls produced no log rows because they
    short-circuited before :func:`apply_agent_action`.
    """
    # Close dedupe (any applied add_comment); describe is naturally
    # gated out by the substantial-description fixture. We do NOT
    # seed a triage row — that's the gate we want OPEN so the
    # scripted priority calls all land in triage.
    _close_other_scenario_gates(safety_setup, dedupe=True)

    # One assistant turn with MANY tool_use blocks, then a text turn
    # to end the loop. AGENT_MAX_ACTIONS+4 ensures the loop has to
    # short-circuit some of them with action_cap_reached.
    over_cap = AGENT_MAX_ACTIONS + 4
    fake_chat.fake.script = [
        [_ToolUse(name="set_priority", input={"priority": "high"}) for _ in range(over_cap)],
        [_TextBlock(text="done")],
    ]

    result = run_agent_body(safety_setup.issue.id)
    assert result["status"] == "ok"
    # Exactly AGENT_MAX_ACTIONS audit rows of set_priority — the
    # surplus tool_uses got action_cap_reached BEFORE the apply layer
    # and so produced no log rows.
    new_priority_rows = AIAgentActionLog.objects.filter(
        issue_id=safety_setup.issue.id, tool_name="set_priority"
    ).count()
    assert new_priority_rows == AGENT_MAX_ACTIONS, (
        f"expected {AGENT_MAX_ACTIONS} priority rows, got {new_priority_rows}"
    )


# ===========================================================================
# 4. Audit completeness
# ===========================================================================


@pytest.mark.django_db
def test_every_action_logged(safety_setup, fake_chat, mock_retrieve):
    """Every tool_use that hits the apply layer produces an audit row,
    regardless of outcome (applied / rejected / error).

    Script: triage emits three tool_uses — one valid set_priority,
    one out-of-range priority (rejected), and one delete_issue (the
    Anthropic SDK would normally refuse but our dispatcher is the
    last line). All three must land in the log.
    """
    # Only close dedupe; describe is naturally gated by the
    # substantial description fixture.
    _close_other_scenario_gates(safety_setup, dedupe=True)

    fake_chat.fake.script = [
        [
            _ToolUse(name="set_priority", input={"priority": "high"}),
            _ToolUse(name="set_priority", input={"priority": "CRITICAL"}),
            _ToolUse(name="delete_issue", input={"issue_id": str(safety_setup.issue.id)}),
        ],
        [_TextBlock(text="done")],
    ]

    run_agent_body(safety_setup.issue.id)

    # The seeded rows from `_close_other_scenario_gates` are NOT
    # set_priority/delete — they're add_comment. Filter to the
    # tool_names emitted by THIS scripted run.
    rows = list(
        AIAgentActionLog.objects.filter(
            issue_id=safety_setup.issue.id,
            tool_name__in=("set_priority", "delete_issue"),
        ).order_by("created_at")
    )
    assert len(rows) == 3
    by_outcome = {}
    for r in rows:
        by_outcome.setdefault(r.status, []).append(r)
    assert len(by_outcome.get(AIAgentActionLog.STATUS_APPLIED, [])) == 1
    # Two rejections: invalid priority + out-of-whitelist tool.
    assert len(by_outcome.get(AIAgentActionLog.STATUS_REJECTED, [])) == 2
    # Each row carries the original input so a reviewer can see
    # exactly what the model tried.
    assert any(r.input.get("priority") == "CRITICAL" for r in rows)
    assert any(r.tool_name == "delete_issue" for r in rows)


# ===========================================================================
# 5. Scenario invariants (Claude mocked)
# ===========================================================================


@pytest.mark.django_db
def test_triage_does_not_hard_assign(
    safety_setup, fake_chat, mock_retrieve, make_user, make_workspace_member
):
    """TZ 5.3 invariant ("предложить ≠ назначить"). A successful
    ``suggest_assignee`` writes an IssueComment by the agent — it
    does NOT add an IssueAssignee row. A wrong auto-assignment is
    the agent's highest-cost failure mode; this test catches a
    regression that flips the comment-mode back to hard-assign.
    """
    from plane.db.models import IssueComment, ProjectMember

    # Close dedupe; describe is auto-gated by substantial description.
    _close_other_scenario_gates(safety_setup, dedupe=True)

    candidate = make_user("triage-candidate")
    make_workspace_member(workspace=safety_setup.workspace, user=candidate, role=15)
    ProjectMember.objects.create(
        workspace=safety_setup.workspace,
        project=safety_setup.project,
        member=candidate,
        role=15,
        is_active=True,
    )

    fake_chat.fake.script = [
        [
            _ToolUse(name="set_priority", input={"priority": "high"}),
            _ToolUse(name="suggest_assignee", input={"user_email": candidate.email}),
        ],
        [_TextBlock(text="done")],
    ]

    run_agent_body(safety_setup.issue.id)

    # Priority did get set (the simpler half of triage).
    safety_setup.issue.refresh_from_db()
    assert safety_setup.issue.priority == "high"
    # Comment by the agent referencing the candidate — present.
    comments = IssueComment.objects.filter(issue_id=safety_setup.issue.id)
    assert any(candidate.email in c.comment_stripped for c in comments)
    # Hard-assign — ABSENT. This is the invariant.
    assert candidate not in list(safety_setup.issue.assignees.all())


@pytest.mark.django_db
def test_dedupe_does_not_close(
    safety_setup, fake_chat, mock_retrieve, make_issue, monkeypatch
):
    """TZ 5.4 invariant: dedupe never closes / merges. The only
    write tools on the dedupe menu are ``add_comment`` and
    ``set_labels``. We make this strong by asserting on what we
    OFFER the model (``DEDUPE_TOOLS``) — closing tools physically
    can't be selected.

    Additionally we drive the scenario end-to-end with a candidate
    and a scripted judge: the comment lands, the label lands, and
    the target issue stays open.
    """
    from ai.dedupe import DEDUPE_LABEL_NAME, DEDUPE_TOOLS
    from plane.db.models import IssueComment

    # The dedupe tool menu — closing/merging is physically absent.
    assert "close_issue" not in DEDUPE_TOOLS
    assert "merge_issue" not in DEDUPE_TOOLS
    assert "delete_issue" not in DEDUPE_TOOLS
    assert set(DEDUPE_TOOLS) == {"add_comment", "set_labels"}

    # Close triage; describe is naturally gated by the substantial
    # description on safety_setup.issue. Don't seed dedupe — that's
    # the scenario we want to drive.
    _close_other_scenario_gates(safety_setup, triage=True)

    # Seed a candidate the dedup scenario will see.
    near = make_issue(
        workspace=safety_setup.workspace,
        project=safety_setup.project,
        name="Same problem from another angle",
    )

    def _fake_candidates(*, issue, agent, cfg, threshold=None):
        return [
            {
                "issue_id": str(near.id),
                "sequence_id": near.sequence_id,
                "name": near.name,
                "distance": 0.10,
            }
        ]

    # The worker imported the helper as ``find_dedupe_candidates`` —
    # patch THAT binding, not ``ai.dedupe.find_candidates``, otherwise
    # the worker's local reference still points at the real function.
    monkeypatch.setattr(agent_worker, "find_dedupe_candidates", _fake_candidates)

    # The "judge" turn picks the candidate and emits both writes.
    fake_chat.fake.script = [
        [
            _ToolUse(
                name="add_comment",
                input={"text": f"Возможные дубли: PROJ-{near.sequence_id}"},
            ),
            _ToolUse(
                name="set_labels", input={"labels": [DEDUPE_LABEL_NAME]}
            ),
        ],
        [_TextBlock(text="done")],
    ]

    run_agent_body(safety_setup.issue.id)

    # Comment landed.
    assert IssueComment.objects.filter(
        issue_id=safety_setup.issue.id,
        comment_stripped__contains=f"PROJ-{near.sequence_id}",
    ).exists()
    # Label attached.
    assert safety_setup.issue.labels.filter(name=DEDUPE_LABEL_NAME).exists()
    # Issue still open — no soft-delete, no archive, no state change.
    safety_setup.issue.refresh_from_db()
    assert safety_setup.issue.deleted_at is None
    near.refresh_from_db()
    assert near.deleted_at is None


@pytest.mark.django_db
def test_describe_does_not_overwrite(
    safety_setup, fake_chat, mock_retrieve, make_issue
):
    """TZ 5.5 invariant: the draft scenario MUST NOT call
    ``update_description``. The draft lands as a comment with the
    visible marker; the user's existing description is left
    untouched.

    We arrange an empty-description issue (so ``should_describe`` is
    True), close triage/dedupe gates so describe is the only path,
    script the model to emit the comment, and assert:
      - the marker comment is present;
      - the issue's description_stripped is unchanged;
      - the tool menu offered to the model did NOT include
        ``update_description`` (the loop-level enforcement).
    """
    from ai.describe import DESCRIBE_MARKER
    from plane.db.models import IssueComment

    # Fresh issue, empty description.
    fresh = make_issue(
        workspace=safety_setup.workspace,
        project=safety_setup.project,
        name="Empty-description trigger",
        description="",
    )
    fresh.assignees.add(
        safety_setup.agent_user,
        through_defaults={
            "workspace_id": safety_setup.workspace.id,
            "project_id": safety_setup.project.id,
        },
    )

    # Close triage / dedupe for THIS issue.
    AIAgentActionLog.objects.create(
        agent=safety_setup.agent,
        workspace_id=safety_setup.workspace.id,
        project_id=safety_setup.project.id,
        issue_id=fresh.id,
        tool_name="set_priority",
        input={"priority": "low"},
        output={"priority": "low"},
        status=AIAgentActionLog.STATUS_APPLIED,
    )
    AIAgentActionLog.objects.create(
        agent=safety_setup.agent,
        workspace_id=safety_setup.workspace.id,
        project_id=safety_setup.project.id,
        issue_id=fresh.id,
        tool_name="add_comment",
        input={"text": "Possible duplicates: PROJ-1"},
        output={"comment_id": "deadbeef", "comment_chars": 30},
        status=AIAgentActionLog.STATUS_APPLIED,
    )

    draft_text = (
        f"{DESCRIBE_MARKER}\n\n"
        "Контекст: тестовый черновик.\n"
        "Шаги: 1, 2, 3.\n"
        "Готово: всё."
    )
    fake_chat.fake.script = [
        [_ToolUse(name="add_comment", input={"text": draft_text})],
        [_TextBlock(text="done")],
    ]

    run_agent_body(fresh.id)

    # Marker comment created.
    assert IssueComment.objects.filter(
        issue_id=fresh.id, comment_stripped__startswith=DESCRIBE_MARKER
    ).exists()
    # Description left empty — the human's content (or its absence)
    # was not overwritten.
    fresh.refresh_from_db()
    assert (fresh.description_stripped or "") == ""

    # Loop-level enforcement: every Claude call made for THIS run
    # offered the tool menu via ``tools=``. None of them should
    # include ``update_description`` — describe deliberately hides it.
    assert fake_chat.fake.calls, "fake Claude was never called"
    for call in fake_chat.fake.calls:
        assert "update_description" not in call["tools"], (
            "update_description leaked into the tool menu — "
            "the describe scenario must hide it"
        )
