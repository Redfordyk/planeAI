"""Map (source_type, source_id) -> plain text suitable for embedding.

Field choices come from [SCHEMA.md](../SCHEMA.md) §db.Issue / §db.IssueComment /
§db.Page:

  work_item ⇒ db.Issue.name + db.Issue.description_stripped
  comment   ⇒ db.IssueComment.comment_stripped
  page      ⇒ db.Page.name + db.Page.description_stripped

`description_stripped` is the plain-text projection of the rich-text
description that Plane stores; we never embed the HTML or BinaryField
representations. For comments, `comment_stripped` is the analogous
field.

Returns `None` when the source is gone, soft-deleted, archived, or
the workspace gate is violated — the caller (reindex_source) treats
None as "nothing to embed, just delete any existing chunks".
"""

from __future__ import annotations

from django.apps import apps


def load_source_text(source_type: str, source_id: str) -> tuple[str, dict] | None:
    """Return `(text, metadata)` or None.

    `metadata` carries fields the caller may want to mirror onto
    DocumentChunk rows (workspace_id, project_id, page-vs-issue
    distinctions). Keeping it as a dict means we can extend without
    breaking signature callers.
    """
    if source_type == "work_item":
        return _load_issue(source_id)
    if source_type == "comment":
        return _load_comment(source_id)
    if source_type == "page":
        return _load_page(source_id)
    return None


def _load_issue(issue_id: str) -> tuple[str, dict] | None:
    Issue = apps.get_model("db", "Issue")
    issue = (
        Issue.objects.filter(id=issue_id, deleted_at__isnull=True, is_draft=False)
        .only("id", "workspace_id", "project_id", "name", "description_stripped")
        .first()
    )
    if issue is None:
        return None
    text = "\n\n".join(p for p in (issue.name, issue.description_stripped or "") if p).strip()
    if not text:
        return None
    return text, {
        "workspace_id": issue.workspace_id,
        "project_id": issue.project_id,
    }


def _load_comment(comment_id: str) -> tuple[str, dict] | None:
    IssueComment = apps.get_model("db", "IssueComment")
    comment = (
        IssueComment.objects.filter(id=comment_id, deleted_at__isnull=True)
        .only("id", "workspace_id", "project_id", "comment_stripped")
        .first()
    )
    if comment is None:
        return None
    text = (comment.comment_stripped or "").strip()
    if not text:
        return None
    return text, {
        "workspace_id": comment.workspace_id,
        "project_id": comment.project_id,
    }


def _load_page(page_id: str) -> tuple[str, dict] | None:
    Page = apps.get_model("db", "Page")
    page = (
        Page.objects.filter(
            id=page_id, deleted_at__isnull=True, archived_at__isnull=True
        )
        .only("id", "workspace_id", "name", "description_stripped")
        .first()
    )
    if page is None:
        return None
    text = "\n\n".join(p for p in (page.name, page.description_stripped or "") if p).strip()
    if not text:
        return None
    # Page has no direct project FK; we store project_id=None on the
    # chunk and filter via ProjectPage at retrieval time (TZ 2.1).
    return text, {
        "workspace_id": page.workspace_id,
        "project_id": None,
    }
