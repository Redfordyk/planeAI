"""TZ 5.4 — duplicate-suggestion scenario invariants.

Locks in the four DoD bullets that don't require a live Anthropic
call:

  - candidate retrieval finds an in-project near-dupe but excludes
    self / cross-project / sub-threshold;
  - the ``possible-duplicate`` label is provisioned lazily and
    idempotently;
  - ``add_comment`` is bound to the issue's project (scope guard);
  - idempotency: a second worker run does not re-post the comment;
  - the agent never closes / merges — the only writes on the dedup
    tool menu are ``add_comment`` and ``set_labels``.

The end-to-end Claude judge call is exercised in TZ 5.7 with a
fully-mocked client; here we instead drive the apply layer
directly when we want to assert a Plane write happened.
"""

from __future__ import annotations

import uuid

import pytest

from ai.agent_worker import apply_agent_action
from ai.dedupe import (
    DEDUPE_DISTANCE_THRESHOLD,
    DEDUPE_LABEL_NAME,
    DEDUPE_TOOLS,
    already_deduped,
    ensure_dedupe_label,
    find_candidates,
)
from ai.models import AIAgentActionLog


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def dedupe_setup(
    db,
    make_user,
    make_workspace,
    make_workspace_member,
    make_project,
    make_ai_config,
    make_issue,
):
    """Workspace with an agent, a project the agent is a member of,
    and a freshly created issue (the "new" one we'd run dedup on)."""
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
        name="Login button broken on mobile",
        description="Tapping the login button does nothing on iOS Safari.",
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


def _patch_retrieve(monkeypatch, chunks):
    """Replace ai.search.retrieve / dedupe.retrieve with a function
    returning the supplied list. We patch through the dedupe module's
    own binding so the helper sees the stub regardless of how the
    name was imported."""
    from ai import dedupe as dedupe_mod

    monkeypatch.setattr(dedupe_mod, "retrieve", lambda **kw: chunks)


def _chunk(*, source_id, project_id, distance, source_type="work_item", content="text"):
    """Build a fake :class:`RetrievedChunk`-shaped object.

    The retriever returns ``RetrievedChunk`` dataclass instances. For
    the dedupe filter we only need a duck-typed object — anything
    with the five attrs the filter reads."""
    class _C:
        pass

    c = _C()
    c.id = str(uuid.uuid4())
    c.source_id = str(source_id)
    c.project_id = str(project_id) if project_id is not None else None
    c.source_type = source_type
    c.content = content
    c.distance = distance
    c.workspace_id = ""
    c.chunk_index = 0
    return c


# ---------------------------------------------------------------------------
# DEDUPE_TOOLS: no closing / no merging
# ---------------------------------------------------------------------------


def test_dedupe_tools_subset_only_safe_writes():
    """The TZ 5.4 hard invariant: dedupe never closes or merges. The
    only writes on the menu are a comment + a marker label."""
    assert set(DEDUPE_TOOLS) == {"add_comment", "set_labels"}
    # Belt: nothing destructive accidentally smuggled in.
    for forbidden in ("delete_issue", "close_issue", "merge_issue", "update_description"):
        assert forbidden not in DEDUPE_TOOLS


# ---------------------------------------------------------------------------
# find_candidates filtering
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_find_candidates_returns_nearby_inproject_workitem(
    dedupe_setup, make_issue, monkeypatch
):
    """A genuinely similar issue in the same project, with cosine
    distance below threshold, becomes a candidate."""
    near = make_issue(
        workspace=dedupe_setup.workspace,
        project=dedupe_setup.project,
        name="Cannot click sign-in on iPhone",
    )
    chunks = [
        _chunk(
            source_id=near.id,
            project_id=dedupe_setup.project.id,
            distance=DEDUPE_DISTANCE_THRESHOLD - 0.05,
        ),
    ]
    _patch_retrieve(monkeypatch, chunks)

    candidates = find_candidates(
        issue=dedupe_setup.issue, agent=dedupe_setup.agent, cfg=dedupe_setup.cfg
    )
    assert len(candidates) == 1
    assert candidates[0]["issue_id"] == str(near.id)
    assert candidates[0]["sequence_id"] == near.sequence_id
    assert candidates[0]["name"] == near.name


@pytest.mark.django_db
def test_find_candidates_excludes_self(dedupe_setup, monkeypatch):
    """A chunk that points back at the issue we're triaging must not
    show up as its own dupe."""
    chunks = [
        _chunk(
            source_id=dedupe_setup.issue.id,
            project_id=dedupe_setup.project.id,
            distance=0.0,
        ),
    ]
    _patch_retrieve(monkeypatch, chunks)
    assert find_candidates(
        issue=dedupe_setup.issue, agent=dedupe_setup.agent, cfg=dedupe_setup.cfg
    ) == []


@pytest.mark.django_db
def test_find_candidates_excludes_cross_project(
    dedupe_setup, make_project, make_issue, monkeypatch
):
    """Even if retrieve returned a chunk from another project (it
    shouldn't, because the ACL filter is project-scoped — but a bug
    upstream would be silent), find_candidates re-filters by
    ``issue.project_id``."""
    other_project = make_project(
        workspace=dedupe_setup.workspace, created_by=dedupe_setup.owner
    )
    other_issue = make_issue(
        workspace=dedupe_setup.workspace,
        project=other_project,
        name="Different project bug",
    )
    chunks = [
        _chunk(
            source_id=other_issue.id,
            project_id=other_project.id,
            distance=0.0,
        ),
    ]
    _patch_retrieve(monkeypatch, chunks)
    assert find_candidates(
        issue=dedupe_setup.issue, agent=dedupe_setup.agent, cfg=dedupe_setup.cfg
    ) == []


@pytest.mark.django_db
def test_find_candidates_respects_threshold(
    dedupe_setup, make_issue, monkeypatch
):
    """A chunk above the cosine-distance threshold is dropped — too
    dissimilar to flag as a duplicate."""
    far = make_issue(
        workspace=dedupe_setup.workspace,
        project=dedupe_setup.project,
        name="Totally unrelated thing",
    )
    chunks = [
        _chunk(
            source_id=far.id,
            project_id=dedupe_setup.project.id,
            distance=DEDUPE_DISTANCE_THRESHOLD + 0.10,
        ),
    ]
    _patch_retrieve(monkeypatch, chunks)
    assert find_candidates(
        issue=dedupe_setup.issue, agent=dedupe_setup.agent, cfg=dedupe_setup.cfg
    ) == []


@pytest.mark.django_db
def test_find_candidates_excludes_non_workitem_sources(
    dedupe_setup, make_issue, monkeypatch
):
    """A close-by *comment* chunk is not a duplicate-issue
    candidate, even when the embedding distance is small."""
    near = make_issue(
        workspace=dedupe_setup.workspace,
        project=dedupe_setup.project,
        name="A related issue",
    )
    chunks = [
        _chunk(
            source_id=near.id,
            project_id=dedupe_setup.project.id,
            distance=0.0,
            source_type="comment",
        ),
    ]
    _patch_retrieve(monkeypatch, chunks)
    assert find_candidates(
        issue=dedupe_setup.issue, agent=dedupe_setup.agent, cfg=dedupe_setup.cfg
    ) == []


@pytest.mark.django_db
def test_find_candidates_dedups_by_issue(dedupe_setup, make_issue, monkeypatch):
    """Multiple chunks for the same near-dupe issue collapse to one
    candidate — the comment shouldn't list "PROJ-42, PROJ-42, PROJ-42"."""
    near = make_issue(
        workspace=dedupe_setup.workspace,
        project=dedupe_setup.project,
        name="Same issue, many chunks",
    )
    chunks = [
        _chunk(source_id=near.id, project_id=dedupe_setup.project.id, distance=0.05),
        _chunk(source_id=near.id, project_id=dedupe_setup.project.id, distance=0.10),
        _chunk(source_id=near.id, project_id=dedupe_setup.project.id, distance=0.15),
    ]
    _patch_retrieve(monkeypatch, chunks)
    result = find_candidates(
        issue=dedupe_setup.issue, agent=dedupe_setup.agent, cfg=dedupe_setup.cfg
    )
    assert len(result) == 1
    assert result[0]["issue_id"] == str(near.id)


# ---------------------------------------------------------------------------
# Label provisioning
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_ensure_dedupe_label_creates_then_is_idempotent(dedupe_setup):
    """First call creates the marker label; second call returns the
    same row without raising. Important when many issues get
    deduplicated within one project — we don't want a race-y
    duplicate label."""
    from plane.db.models import Label

    label1, created1 = ensure_dedupe_label(
        workspace=dedupe_setup.workspace, project=dedupe_setup.project
    )
    assert created1 is True
    assert label1.name == DEDUPE_LABEL_NAME

    label2, created2 = ensure_dedupe_label(
        workspace=dedupe_setup.workspace, project=dedupe_setup.project
    )
    assert created2 is False
    assert label2.id == label1.id

    assert (
        Label.objects.filter(
            project=dedupe_setup.project, name=DEDUPE_LABEL_NAME
        ).count()
        == 1
    )


# ---------------------------------------------------------------------------
# add_comment apply: scope guard + cap
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_add_comment_creates_comment_authored_by_agent(dedupe_setup):
    """Happy path: comment lands, authored by the agent user, bound
    to the issue's project."""
    from plane.db.models import IssueComment

    log = apply_agent_action(
        agent=dedupe_setup.agent,
        issue=dedupe_setup.issue,
        tool_name="add_comment",
        tool_input={"text": "Possible duplicates: PROJ-1, PROJ-2"},
        cfg=dedupe_setup.cfg,
    )
    assert log.status == AIAgentActionLog.STATUS_APPLIED
    comment = IssueComment.objects.get(id=log.output["comment_id"])
    assert comment.actor_id == dedupe_setup.agent_user.id
    assert comment.issue_id == dedupe_setup.issue.id
    assert comment.project_id == dedupe_setup.project.id
    assert "PROJ-1" in comment.comment_stripped


@pytest.mark.django_db
def test_add_comment_rejects_empty_and_oversized(dedupe_setup):
    """Empty and over-cap text both reject — neither produces an
    IssueComment row."""
    from plane.db.models import IssueComment
    from ai.agent_worker import MAX_COMMENT_CHARS

    log_empty = apply_agent_action(
        agent=dedupe_setup.agent,
        issue=dedupe_setup.issue,
        tool_name="add_comment",
        tool_input={"text": "   "},
        cfg=dedupe_setup.cfg,
    )
    assert log_empty.status == AIAgentActionLog.STATUS_REJECTED

    log_big = apply_agent_action(
        agent=dedupe_setup.agent,
        issue=dedupe_setup.issue,
        tool_name="add_comment",
        tool_input={"text": "x" * (MAX_COMMENT_CHARS + 1)},
        cfg=dedupe_setup.cfg,
    )
    assert log_big.status == AIAgentActionLog.STATUS_REJECTED
    assert IssueComment.objects.filter(issue_id=dedupe_setup.issue.id).count() == 0


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_already_deduped_flips_after_applied_comment(dedupe_setup):
    """The dedup idempotency predicate flips when *the comment*
    lands — that's the visible artifact the second run mustn't
    duplicate."""
    assert already_deduped(dedupe_setup.issue.id) is False
    apply_agent_action(
        agent=dedupe_setup.agent,
        issue=dedupe_setup.issue,
        tool_name="add_comment",
        tool_input={"text": "Possible duplicates: PROJ-1"},
        cfg=dedupe_setup.cfg,
    )
    assert already_deduped(dedupe_setup.issue.id) is True


@pytest.mark.django_db
def test_already_deduped_ignores_rejected_comments(dedupe_setup):
    """A rejected add_comment (empty / oversize) must NOT flip the
    gate. The comment never actually appeared."""
    apply_agent_action(
        agent=dedupe_setup.agent,
        issue=dedupe_setup.issue,
        tool_name="add_comment",
        tool_input={"text": ""},
        cfg=dedupe_setup.cfg,
    )
    assert already_deduped(dedupe_setup.issue.id) is False
