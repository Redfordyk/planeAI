"""DRF views for Angela (mounted under /api/ai/workspaces/<ws>/angela/).

Endpoints:

  GET    runs/                 list recent Angela runs in the workspace
  POST   runs/                 start a run (issue_id|prompt, deploy_mode, target)
  GET    runs/<id>/            run detail + ordered step feed
  POST   runs/<id>/approve/    [staging_gate] approve → prod deploy
  POST   runs/<id>/deploy/     [manual] ship the green artifact now
  POST   runs/<id>/cancel/     mark a non-terminal run cancelled
  POST   docs/                 generate docs for a target → (local) wiki
  GET    targets/              allow-listed sandbox targets + deploy modes

ACL: every endpoint requires workspace membership; mutating endpoints
also require AI enabled + within budget (``_user_can_use_ai``). The
client never supplies a repo URL — only a logical ``target`` key that
the config allow-list resolves (sandbox isolation).
"""

from __future__ import annotations

import io
import logging
import zipfile

from django.http import HttpResponse
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from ai.models import AngelaRun, AngelaStep
from ai.views import _is_workspace_member, _user_can_use_ai

from . import config
from .tasks import angela_approve_prod, angela_manual_deploy, angela_run


logger = logging.getLogger("plane.ai.angela.api")


_VALID_MODES = {
    AngelaRun.MODE_STAGING_GATE,
    AngelaRun.MODE_AUTONOMOUS_PROD,
    AngelaRun.MODE_MANUAL,
}


def _run_to_dict(r: AngelaRun, *, steps: bool = False) -> dict:
    d = {
        "id": str(r.id),
        "workspace_id": str(r.workspace_id),
        "project_id": str(r.project_id) if r.project_id else None,
        "issue_id": str(r.issue_id) if r.issue_id else None,
        "parent_run_id": str(r.parent_run_id) if r.parent_run_id else None,
        "title": r.title,
        "target_repo": r.target_repo,
        "prompt": r.prompt,
        "deploy_mode": r.deploy_mode,
        "status": r.status,
        "branch": r.branch,
        "review_verdict": r.review_verdict,
        "test_passed": r.test_passed,
        "test_summary": r.test_summary,
        "iterations": r.iterations,
        "deploy_target": r.deploy_target,
        "deploy_url": r.deploy_url,
        "wiki_url": r.wiki_url,
        "error": r.error,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }
    if steps:
        d["steps"] = [
            {
                "id": str(s.id),
                "phase": s.phase,
                "status": s.status,
                "title": s.title,
                "detail": s.detail,
                "iteration": s.iteration,
                "created_at": s.created_at.isoformat(),
            }
            for s in r.steps.order_by("created_at")
        ]
    return d


class AngelaTargetsView(APIView):
    """GET allow-listed sandbox targets + the three deploy modes."""

    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id):
        if not _is_workspace_member(request.user, workspace_id):
            return Response({"error": "not_a_member"}, status=403)
        return Response({
            "targets": config.list_targets(),
            "default_target": config.default_target(),
            "deploy_modes": [
                {"key": AngelaRun.MODE_STAGING_GATE,
                 "label": "Auto → staging, approve for prod"},
                {"key": AngelaRun.MODE_AUTONOMOUS_PROD,
                 "label": "Fully autonomous → prod"},
                {"key": AngelaRun.MODE_MANUAL,
                 "label": "Pipeline only, deploy by hand"},
            ],
        })


class AngelaRunListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id):
        if not _is_workspace_member(request.user, workspace_id):
            return Response({"error": "not_a_member"}, status=403)
        qs = AngelaRun.objects.filter(workspace_id=workspace_id)
        issue_id = request.query_params.get("issue_id")
        if issue_id:
            qs = qs.filter(issue_id=issue_id)
        runs = qs.order_by("-created_at")[:50]
        return Response({"runs": [_run_to_dict(r) for r in runs]})

    def post(self, request, workspace_id):
        ok, err, _cfg, http = _user_can_use_ai(request.user, workspace_id)
        if not ok:
            return Response({"error": err}, status=http)

        data = request.data if isinstance(request.data, dict) else {}
        prompt = str(data.get("prompt") or "").strip()
        issue_id = data.get("issue_id") or None
        if not prompt and not issue_id:
            return Response({"error": "prompt or issue_id required"}, status=400)

        deploy_mode = data.get("deploy_mode") or AngelaRun.MODE_STAGING_GATE
        if deploy_mode not in _VALID_MODES:
            return Response({"error": f"invalid deploy_mode: {deploy_mode}"}, status=400)

        target = data.get("target") or config.default_target()
        try:
            config.resolve_target(target)
        except config.AngelaConfigError as exc:
            return Response({"error": str(exc)}, status=400)

        run = AngelaRun.objects.create(
            workspace_id=workspace_id,
            project_id=data.get("project_id") or None,
            issue_id=issue_id,
            target_repo=target,
            prompt=prompt,
            deploy_mode=deploy_mode,
            status=AngelaRun.STATUS_QUEUED,
            created_by=request.user,
        )
        # Fire-and-forget; the loop runs in a worker.
        angela_run.apply_async(args=[str(run.id)], kwargs={"run_docs": True})
        return Response(_run_to_dict(run), status=201)


class AngelaRunDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id, run_id):
        if not _is_workspace_member(request.user, workspace_id):
            return Response({"error": "not_a_member"}, status=403)
        run = AngelaRun.objects.filter(id=run_id, workspace_id=workspace_id).first()
        if run is None:
            return Response({"error": "not_found"}, status=404)
        return Response(_run_to_dict(run, steps=True))


class AngelaApproveView(APIView):
    """staging_gate: human approves the prod deploy."""

    permission_classes = [IsAuthenticated]

    def post(self, request, workspace_id, run_id):
        ok, err, _cfg, http = _user_can_use_ai(request.user, workspace_id)
        if not ok:
            return Response({"error": err}, status=http)
        run = AngelaRun.objects.filter(id=run_id, workspace_id=workspace_id).first()
        if run is None:
            return Response({"error": "not_found"}, status=404)
        if run.status != AngelaRun.STATUS_AWAITING_APPROVAL:
            return Response({"error": f"run not awaiting approval (status={run.status})"}, status=409)
        angela_approve_prod.apply_async(args=[str(run.id), str(request.user.id)])
        return Response({"status": "approving"}, status=202)


class AngelaManualDeployView(APIView):
    """manual mode: ship the green artifact via the 'Deploy now' button."""

    permission_classes = [IsAuthenticated]

    def post(self, request, workspace_id, run_id):
        ok, err, _cfg, http = _user_can_use_ai(request.user, workspace_id)
        if not ok:
            return Response({"error": err}, status=http)
        run = AngelaRun.objects.filter(id=run_id, workspace_id=workspace_id).first()
        if run is None:
            return Response({"error": "not_found"}, status=404)
        if run.test_passed is not True:
            return Response({"error": "artifact is not green; nothing to deploy"}, status=409)
        angela_manual_deploy.apply_async(args=[str(run.id), str(request.user.id)])
        return Response({"status": "deploying"}, status=202)


class AngelaCancelView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, workspace_id, run_id):
        if not _is_workspace_member(request.user, workspace_id):
            return Response({"error": "not_a_member"}, status=403)
        run = AngelaRun.objects.filter(id=run_id, workspace_id=workspace_id).first()
        if run is None:
            return Response({"error": "not_found"}, status=404)
        terminal = {AngelaRun.STATUS_SUCCEEDED, AngelaRun.STATUS_FAILED, AngelaRun.STATUS_CANCELLED}
        if run.status in terminal:
            return Response({"error": f"run already {run.status}"}, status=409)
        run.status = AngelaRun.STATUS_CANCELLED
        run.save(update_fields=["status", "updated_at"])
        AngelaStep.objects.create(
            run=run, workspace_id=run.workspace_id, phase=AngelaStep.PHASE_PLAN,
            status=AngelaStep.STATUS_SKIPPED, title="cancelled by user",
        )
        return Response(_run_to_dict(run))


class AngelaDocsView(APIView):
    """Generate documentation for a sandbox target and push to the wiki.

    Runs a throwaway sandbox checkout + docgen synchronously-ish via a
    worker task is overkill for the demo; we enqueue a one-off run with
    a docs-only flag instead, reusing the pipeline's docs phase.
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, workspace_id):
        ok, err, _cfg, http = _user_can_use_ai(request.user, workspace_id)
        if not ok:
            return Response({"error": err}, status=http)
        data = request.data if isinstance(request.data, dict) else {}
        target = data.get("target") or config.default_target()
        try:
            config.resolve_target(target)
        except config.AngelaConfigError as exc:
            return Response({"error": str(exc)}, status=400)
        # A docs-only run: empty prompt is allowed here because we set a
        # sentinel prompt and mark it manual so no deploy happens; the
        # pipeline still clones, then we only care about the docs phase.
        run = AngelaRun.objects.create(
            workspace_id=workspace_id,
            project_id=data.get("project_id") or None,
            target_repo=target,
            prompt="(docs-only) Сгенерировать документацию проекта в вики.",
            deploy_mode=AngelaRun.MODE_MANUAL,
            status=AngelaRun.STATUS_QUEUED,
            created_by=request.user,
        )
        from .tasks import angela_run as _run
        _run.apply_async(args=[str(run.id)], kwargs={"run_docs": True})
        return Response(_run_to_dict(run), status=201)


class AngelaRefineView(APIView):
    """POST runs/<id>/refine/ — create a child run that edits the parent's
    result with a new instruction ("доработать"). Body: { prompt }."""

    permission_classes = [IsAuthenticated]

    def post(self, request, workspace_id, run_id):
        ok, err, _cfg, http = _user_can_use_ai(request.user, workspace_id)
        if not ok:
            return Response({"error": err}, status=http)
        parent = AngelaRun.objects.filter(id=run_id, workspace_id=workspace_id).first()
        if parent is None:
            return Response({"error": "not_found"}, status=404)
        data = request.data if isinstance(request.data, dict) else {}
        prompt = str(data.get("prompt") or "").strip()
        if not prompt:
            return Response({"error": "prompt required"}, status=400)
        # The parent must have produced a published artifact to build on.
        if not (config.artifacts_dir() / str(parent.id)).exists():
            return Response({"error": "parent has no artifact to refine"}, status=409)

        child = AngelaRun.objects.create(
            workspace_id=workspace_id,
            project_id=parent.project_id,
            parent_run_id=parent.id,
            title=parent.title,
            target_repo=parent.target_repo,
            prompt=prompt,
            deploy_mode=parent.deploy_mode or AngelaRun.MODE_AUTONOMOUS_PROD,
            status=AngelaRun.STATUS_QUEUED,
            created_by=request.user,
        )
        angela_run.apply_async(args=[str(child.id)], kwargs={"run_docs": False})
        return Response(_run_to_dict(child), status=201)


class AngelaDownloadView(APIView):
    """GET runs/<id>/download/ — stream the published artifact as a zip."""

    permission_classes = [IsAuthenticated]

    def get(self, request, workspace_id, run_id):
        if not _is_workspace_member(request.user, workspace_id):
            return Response({"error": "not_a_member"}, status=403)
        run = AngelaRun.objects.filter(id=run_id, workspace_id=workspace_id).first()
        if run is None:
            return Response({"error": "not_found"}, status=404)
        root = config.artifacts_dir() / str(run.id)
        if not root.exists():
            return Response({"error": "no artifact for this run"}, status=404)

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for item in root.rglob("*"):
                if any(p in (".git", "__pycache__", ".pytest_cache") for p in item.parts):
                    continue
                if item.is_file():
                    zf.write(item, item.relative_to(root).as_posix())
        buf.seek(0)
        name = (run.title or "angela-project").strip().replace(" ", "-")[:40] or "angela-project"
        resp = HttpResponse(buf.getvalue(), content_type="application/zip")
        resp["Content-Disposition"] = f'attachment; filename="{name}-{str(run.id)[:8]}.zip"'
        return resp
