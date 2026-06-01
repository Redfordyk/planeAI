"""Angela's end-to-end run orchestrator.

Ties the phases together into the bounded autonomous loop:

    code → self-review →(loop until approved / cap)→ test
         →(loop until green / cap)→ deploy(by mode) → docs

State is persisted on the ``AngelaRun`` row and streamed as
``AngelaStep`` rows so the console feed is live. The loop is bounded by
``config.max_fix_iterations()`` on BOTH the review and the test gates,
so Angela can never spin forever on a stuck plan (same discipline as
``agent_loop.MAX_TURNS``).

This module is synchronous and is meant to be invoked from a Celery
task (``ai.angela.tasks`` / reuse ``ai.tasks``). The API view enqueues
it; it never runs inside the request thread.

Safety: the *view* has already checked workspace membership + AI budget
before this runs. The only writable filesystem target is the sandbox
checkout. The issue text is untrusted and only ever enters LLM calls in
the ``user`` role.
"""

from __future__ import annotations

import logging

from ai.models import AngelaRun, AngelaStep

from . import coder, deployer, docgen, intake, reviewer
from .base import fail_run, log_step, set_status
from .config import AngelaConfigError, max_fix_iterations, resolve_target
from .sandbox import Sandbox, open_sandbox
from .tester import run_tests


logger = logging.getLogger("plane.ai.angela.pipeline")


def run_pipeline(run_id: str, *, run_docs: bool = True) -> None:
    """Execute the full pipeline for an existing queued ``AngelaRun``."""
    run = AngelaRun.objects.filter(id=run_id).first()
    if run is None:
        logger.warning("angela pipeline: run %s not found", run_id)
        return
    if run.status not in (AngelaRun.STATUS_QUEUED,):
        logger.info("angela pipeline: run %s already in status %s; skipping", run_id, run.status)
        return

    model = _model_for(run.workspace_id)
    sandbox: Sandbox | None = None
    try:
        # --- prepare sandbox ----------------------------------------
        log_step(run, phase=AngelaStep.PHASE_PLAN, status=AngelaStep.STATUS_STARTED,
                 title=f"prepare sandbox '{run.target_repo}'")
        try:
            sandbox = open_sandbox(str(run.id), run.target_repo)
        except AngelaConfigError as exc:
            fail_run(run, phase=AngelaStep.PHASE_PLAN, error=str(exc))
            return
        branch = sandbox.create_branch(f"angela/run-{str(run.id)[:8]}")
        set_status(run, AngelaRun.STATUS_CODING, branch=branch)
        log_step(run, phase=AngelaStep.PHASE_PLAN, status=AngelaStep.STATUS_OK,
                 title=f"sandbox ready on branch {branch}")

        existing_files = None
        if run.parent_run_id:
            # --- refinement: seed from the parent's published artifact --
            from .config import artifacts_dir
            n = sandbox.seed_from(artifacts_dir() / str(run.parent_run_id))
            sandbox.stage_all()
            sandbox.commit("seed from parent run")
            existing_files = sandbox.snapshot()
            log_step(run, phase=AngelaStep.PHASE_PLAN, status=AngelaStep.STATUS_OK,
                     title=f"взяла прошлый проект ({n} файлов) и дорабатываю")
            issue_text = (
                "Доработай СУЩЕСТВУЮЩИЙ проект (его файлы приведены ниже). "
                "Внеси изменение пользователя: " + (run.prompt or "").strip()
                + ". Сохрани всё остальное рабочим и не ломай вёрстку. "
                "Верни ПОЛНЫЕ обновлённые версии изменённых файлов."
            )
            # Inherit the parent's friendly title.
            if not run.title:
                parent = AngelaRun.objects.filter(id=run.parent_run_id).only("title").first()
                if parent and parent.title:
                    set_status(run, run.status, title=parent.title)
        else:
            # --- intake: expand the casual idea into a real brief -------
            log_step(run, phase=AngelaStep.PHASE_PLAN, status=AngelaStep.STATUS_STARTED,
                     title="понимаю задачу")
            title, brief = intake.expand_brief(
                workspace_id=run.workspace_id, user_id=run.created_by_id,
                idea=run.prompt or "", model=model,
            )
            issue_text = brief or (run.prompt or "")
            if title:
                set_status(run, run.status, title=title)
            log_step(run, phase=AngelaStep.PHASE_PLAN, status=AngelaStep.STATUS_OK,
                     title=(f"поняла задачу: {title}" if title else "поняла задачу так"),
                     detail=brief)

        file_tree = sandbox.file_tree()

        # --- code ↔ self-review loop --------------------------------
        cap = max_fix_iterations()
        review = None
        prior_summary = None
        feedback = None
        diff = ""
        for it in range(1, cap + 1):
            set_status(run, AngelaRun.STATUS_CODING)
            log_step(run, phase=AngelaStep.PHASE_CODE, status=AngelaStep.STATUS_STARTED,
                     title=f"generating code (iteration {it})", iteration=it)
            plan = coder.generate_code(
                workspace_id=run.workspace_id, user_id=run.created_by_id,
                issue_text=issue_text, file_tree=file_tree, model=model,
                review_feedback=feedback, prior_summary=prior_summary,
                existing_files=existing_files,
            )
            if not plan.files:
                log_step(run, phase=AngelaStep.PHASE_CODE, status=AngelaStep.STATUS_FAILED,
                         title="no files produced", detail=plan.summary or plan.raw[:1000], iteration=it)
                if it == cap:
                    fail_run(run, phase=AngelaStep.PHASE_CODE, error="coder produced no files")
                    return
                feedback = "Предыдущая попытка не вернула ни одного файла. Верни корректный JSON со списком files."
                continue

            for f in plan.files:
                sandbox.write_file(f["path"], f["content"])
            sandbox.stage_all()
            diff = sandbox.diff(staged=True)
            log_step(run, phase=AngelaStep.PHASE_CODE, status=AngelaStep.STATUS_OK,
                     title=plan.summary[:160] or f"wrote {len(plan.files)} file(s)",
                     detail="\n".join(f["path"] for f in plan.files), iteration=it)
            prior_summary = plan.summary

            # self-review
            set_status(run, AngelaRun.STATUS_REVIEWING)
            log_step(run, phase=AngelaStep.PHASE_REVIEW, status=AngelaStep.STATUS_STARTED,
                     title="self-review", iteration=it)
            review = reviewer.review_diff(
                workspace_id=run.workspace_id, user_id=run.created_by_id,
                issue_text=issue_text, diff=diff, model=model,
            )
            set_status(run, run.status, review_verdict=review.verdict, diff=diff[:60000])
            log_step(
                run, phase=AngelaStep.PHASE_REVIEW,
                status=AngelaStep.STATUS_OK if review.approved else AngelaStep.STATUS_FAILED,
                title=f"review: {review.verdict} (score {review.score})",
                detail=review.feedback_text(), iteration=it,
            )
            if review.approved:
                break
            feedback = review.feedback_text()
            # reset the working tree for a clean re-generation next loop
            sandbox._git("reset", "--hard", "HEAD", allow_fail=True)  # noqa: SLF001
        run.iterations = it
        run.save(update_fields=["iterations", "updated_at"])

        if review is None or not review.approved:
            fail_run(run, phase=AngelaStep.PHASE_REVIEW,
                     error="self-review never approved within iteration cap")
            return

        sandbox.commit(f"Angela: {prior_summary or 'changes'} (run {str(run.id)[:8]})")

        # --- test loop ----------------------------------------------
        set_status(run, AngelaRun.STATUS_TESTING)
        log_step(run, phase=AngelaStep.PHASE_TEST, status=AngelaStep.STATUS_STARTED, title="running tests")
        result = run_tests(sandbox)
        log_step(
            run, phase=AngelaStep.PHASE_TEST,
            status=AngelaStep.STATUS_OK if result.passed else AngelaStep.STATUS_FAILED,
            title=f"tests: {result.summary}", detail=result.output_tail,
        )
        # one fix attempt on red tests, feeding failures back to the coder
        if not result.passed:
            for it in range(run.iterations + 1, run.iterations + cap + 1):
                set_status(run, AngelaRun.STATUS_CODING)
                log_step(run, phase=AngelaStep.PHASE_CODE, status=AngelaStep.STATUS_STARTED,
                         title=f"fixing failing tests (iteration {it})", iteration=it)
                plan = coder.generate_code(
                    workspace_id=run.workspace_id, user_id=run.created_by_id,
                    issue_text=issue_text, file_tree=sandbox.file_tree(), model=model,
                    review_feedback="Тесты падают. Вывод:\n" + result.output_tail[-3000:],
                    prior_summary=prior_summary,
                )
                for f in plan.files:
                    sandbox.write_file(f["path"], f["content"])
                sandbox.stage_all()
                sandbox.commit(f"Angela fix tests (run {str(run.id)[:8]} it{it})")
                set_status(run, AngelaRun.STATUS_TESTING)
                result = run_tests(sandbox)
                log_step(
                    run, phase=AngelaStep.PHASE_TEST,
                    status=AngelaStep.STATUS_OK if result.passed else AngelaStep.STATUS_FAILED,
                    title=f"tests: {result.summary}", detail=result.output_tail, iteration=it,
                )
                run.iterations = it
                run.save(update_fields=["iterations", "updated_at"])
                if result.passed:
                    break

        set_status(run, run.status, test_passed=result.passed, test_summary=result.summary[:2000],
                   diff=sandbox.diff(staged=False) or run.diff)
        if not result.passed:
            fail_run(run, phase=AngelaStep.PHASE_TEST,
                     error="tests still failing after fix attempts:\n" + result.output_tail[-2000:])
            return

        # --- deploy (by mode) ---------------------------------------
        set_status(run, AngelaRun.STATUS_DEPLOYING)
        deployer.run_deploy(run, sandbox)

        # --- verify the deployed page actually opens ----------------
        _verify_reachable(run)

        # --- docs (best-effort) -------------------------------------
        if run_docs:
            _try_docs(run, sandbox, model)

        # Keep the sandbox checkout around if we're waiting on a human
        # (the prod deploy reuses it); otherwise clean up.
        run.refresh_from_db(fields=["status"])
        if run.status != AngelaRun.STATUS_AWAITING_APPROVAL and sandbox is not None:
            sandbox.cleanup()

    except Exception as exc:  # noqa: BLE001 — last-resort guard
        logger.exception("angela pipeline crashed for run %s", run_id)
        try:
            fail_run(run, phase=AngelaStep.PHASE_PLAN, error=f"pipeline crash: {exc}")
        finally:
            if sandbox is not None:
                sandbox.cleanup()


def _verify_reachable(run: AngelaRun) -> None:
    """Best-effort: fetch the deployed URL and confirm the page actually
    opens (200 + non-trivial HTML). Logged as a step; never fails the run
    — the artifact already exists and a fetch hiccup shouldn't undo a
    green build."""
    run.refresh_from_db(fields=["status", "deploy_url"])
    if run.status != AngelaRun.STATUS_SUCCEEDED or not run.deploy_url:
        return
    try:
        import requests

        resp = requests.get(run.deploy_url, timeout=15)
        body = resp.content or b""
        size = len(body)
        # nginx autoindex (a project with no index.html, e.g. a Python
        # script) → that's not a "web page", the deliverable is the ZIP.
        is_dir_listing = b"<title>Index of /" in body[:400] or b"<h1>Index of /" in body[:600]
        ok = resp.status_code == 200 and size > 200 and b"<" in body
        if resp.status_code == 200 and is_dir_listing:
            log_step(run, phase=AngelaStep.PHASE_DEPLOY, status=AngelaStep.STATUS_OK,
                     title="проект без веб-страницы — скачайте ZIP",
                     detail="Это код/файлы, а не сайт. Готовый проект доступен по кнопке «Скачать ZIP».")
        elif ok:
            log_step(run, phase=AngelaStep.PHASE_DEPLOY, status=AngelaStep.STATUS_OK,
                     title=f"страница открывается ✓ ({size / 1024:.1f} КБ)",
                     detail=f"GET {run.deploy_url} → {resp.status_code}, {size} bytes")
        else:
            log_step(run, phase=AngelaStep.PHASE_DEPLOY, status=AngelaStep.STATUS_SKIPPED,
                     title="страница ответила неожиданно",
                     detail=f"GET {run.deploy_url} → {resp.status_code}, {size} bytes")
    except Exception as exc:  # noqa: BLE001 — verification is best-effort
        log_step(run, phase=AngelaStep.PHASE_DEPLOY, status=AngelaStep.STATUS_SKIPPED,
                 title="не смогла проверить открытие страницы", detail=str(exc)[:300])


def approve_prod_deploy(run_id: str, approver_id) -> bool:
    """Run the gated prod deploy for a run sitting in awaiting_approval.

    Returns True if the deploy was attempted. The sandbox checkout from
    the original run is reused (it was preserved for this purpose).
    """
    run = AngelaRun.objects.filter(id=run_id, status=AngelaRun.STATUS_AWAITING_APPROVAL).first()
    if run is None:
        return False
    target = resolve_target(run.target_repo)
    sandbox = Sandbox(str(run.id), target)
    if not sandbox.root.exists():
        # checkout was cleaned up — re-prepare and re-apply nothing; the
        # branch is gone, so we can only redeploy current default.
        log_step(run, phase=AngelaStep.PHASE_DEPLOY, status=AngelaStep.STATUS_FAILED,
                 title="sandbox checkout no longer present",
                 detail="Re-run Angela to regenerate the artifact before deploying.")
        set_status(run, AngelaRun.STATUS_FAILED, error="sandbox gone before approval")
        return True
    set_status(run, AngelaRun.STATUS_DEPLOYING, approved_by_id=approver_id)
    out = deployer.deploy_prod(run, sandbox)
    if out.ok:
        set_status(run, AngelaRun.STATUS_SUCCEEDED, deploy_target="prod", deploy_url=out.url)
    else:
        set_status(run, AngelaRun.STATUS_FAILED, deploy_target="prod",
                   error="prod deploy failed:\n" + out.detail[:2000])
    sandbox.cleanup()
    return True


def manual_deploy(run_id: str, deployer_id) -> bool:
    """Ship a green artifact from MODE_MANUAL via the 'Deploy now' button."""
    run = AngelaRun.objects.filter(id=run_id).first()
    if run is None or run.test_passed is not True:
        return False
    target = resolve_target(run.target_repo)
    sandbox = Sandbox(str(run.id), target)
    if not sandbox.root.exists():
        sandbox.prepare()
    set_status(run, AngelaRun.STATUS_DEPLOYING, approved_by_id=deployer_id)
    out = deployer.deploy_prod(run, sandbox)
    if out.ok:
        set_status(run, AngelaRun.STATUS_SUCCEEDED, deploy_target="prod", deploy_url=out.url)
    else:
        set_status(run, AngelaRun.STATUS_FAILED, deploy_target="prod",
                   error="manual deploy failed:\n" + out.detail[:2000])
    sandbox.cleanup()
    return True


def _try_docs(run: AngelaRun, sandbox: Sandbox, model: str) -> None:
    log_step(run, phase=AngelaStep.PHASE_DOCS, status=AngelaStep.STATUS_STARTED, title="generating docs → wiki")
    try:
        res = docgen.generate_docs(
            workspace_id=run.workspace_id, user_id=run.created_by_id,
            repo_root=sandbox.root, model=model,
            page_title=f"{sandbox.target.key}-{str(run.id)[:8]}",
        )
        if res.get("wiki_url"):
            run.wiki_url = res["wiki_url"]
            run.save(update_fields=["wiki_url", "updated_at"])
        log_step(run, phase=AngelaStep.PHASE_DOCS, status=AngelaStep.STATUS_OK,
                 title=res.get("note", "docs generated"),
                 detail=f"{res.get('files', 0)} files documented. {res.get('wiki_url','')}")
    except Exception as exc:  # noqa: BLE001 — docs never fail the run
        log_step(run, phase=AngelaStep.PHASE_DOCS, status=AngelaStep.STATUS_FAILED,
                 title="docs generation failed", detail=str(exc))


def _model_for(workspace_id) -> str:
    from ai.models import WorkspaceAIConfig
    from ai.providers import CHAT_MODEL
    cfg = WorkspaceAIConfig.objects.filter(workspace_id=workspace_id).only("chat_model").first()
    return (cfg.chat_model if cfg and cfg.chat_model else CHAT_MODEL)
