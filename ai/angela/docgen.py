"""Documentation generation for Angela (autodoc lineage → MediaWiki).

Port of the standalone ``autodoc`` prototype, rewired to:
  - analyse the *sandbox checkout* (not an arbitrary host path),
  - generate prose via our config-driven ``get_chat`` (Claude) with
    usage billed through ``record_usage`` (not a raw OpenAI client),
  - publish to a (typically locally-hosted) MediaWiki declared in
    ``settings.ANGELA["WIKI"]``.

If the wiki is disabled or unreachable, we still return the rendered
wikitext so the caller can surface / download it — the docs are never
lost just because the wiki is offline. Per the product decision, the
wiki runs on the PC, not the server.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path
from typing import Optional

import requests

from ai.models import AIUsageLog
from ai.providers import get_chat
from ai.usage import record_usage

from .config import wiki_config


logger = logging.getLogger("plane.ai.angela.docgen")

_IGNORE_DIRS = {".venv", "venv", "__pycache__", "site-packages", ".git", ".idea", "node_modules"}


# --------------------------------------------------------------------------
# AST analysis
# --------------------------------------------------------------------------


def analyze_project(root: Path) -> dict:
    """Walk a Python project and return a structure tree.

    {name, files:[{path, classes:[{name, doc, methods:[...]}], functions:[...]}]}
    """
    root = Path(root)
    structure: dict = {"name": root.name, "files": []}
    for py in sorted(root.rglob("*.py")):
        if any(part in _IGNORE_DIRS for part in py.parts):
            continue
        if py.name == "__init__.py":
            continue
        fdata = _analyze_file(py, root)
        if fdata:
            structure["files"].append(fdata)
    return structure


def _analyze_file(path: Path, root: Path) -> Optional[dict]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))
    except (SyntaxError, ValueError) as exc:
        logger.info("docgen: skip %s (%s)", path, exc)
        return None
    classes, functions = [], []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            classes.append({
                "name": node.name,
                "doc": ast.get_docstring(node) or "",
                "methods": [
                    {"name": m.name, "doc": ast.get_docstring(m) or "",
                     "args": [a.arg for a in m.args.args]}
                    for m in node.body if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
                ],
            })
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append({
                "name": node.name,
                "doc": ast.get_docstring(node) or "",
                "args": [a.arg for a in node.args.args],
            })
    if not classes and not functions:
        return None
    return {
        "path": str(path.relative_to(root)),
        "classes": classes,
        "functions": functions,
    }


# --------------------------------------------------------------------------
# Wikitext rendering (+ optional LLM overview)
# --------------------------------------------------------------------------


def _overview_via_llm(workspace_id, user_id, model: str, structure: dict) -> str:
    """One short LLM-written project overview. Best-effort; empty on failure."""
    try:
        chat = get_chat(workspace_id)
        names = ", ".join(f["path"] for f in structure["files"][:40])
        msg = chat.complete(
            system="Ты — технический писатель. Дай краткое (3-5 предложений) описание "
                   "Python-проекта по списку его файлов и классов. Только текст.",
            messages=[{"role": "user", "content": f"Проект {structure['name']}. Файлы: {names}"}],
            model=model,
            max_tokens=400,
            temperature=0.2,
        )
        record_usage(
            workspace_id=workspace_id, user_id=user_id,
            feature=AIUsageLog.FEATURE_AGENT, model=model,
            usage=getattr(msg, "usage", {}),
        )
        parts = [b.text for b in getattr(msg, "content", []) if getattr(b, "type", None) == "text"]
        return "".join(parts).strip()
    except Exception as exc:  # noqa: BLE001 — overview is optional
        logger.info("docgen: overview LLM failed: %s", exc)
        return ""


def render_wikitext(structure: dict, overview: str = "") -> str:
    name = structure["name"]
    out = ["__NOEDITSECTION__", f"= {name} =", "__TOC__", ""]
    if overview:
        out += ["== Описание ==", overview, ""]
    out.append("== Структура ==")
    for f in structure["files"]:
        out.append(f"=== <code>{f['path']}</code> ===")
        for cls in f["classes"]:
            out.append(f"==== Класс {cls['name']} ====")
            if cls["doc"]:
                out.append(cls["doc"])
            for m in cls["methods"]:
                out.append(f"* <code>{m['name']}({', '.join(m['args'])})</code> — {m['doc'][:160]}")
        for fn in f["functions"]:
            out.append(f"* функция <code>{fn['name']}({', '.join(fn['args'])})</code> — {fn['doc'][:160]}")
        out.append("")
    return "\n".join(out)


# --------------------------------------------------------------------------
# MediaWiki publishing
# --------------------------------------------------------------------------


class _Wiki:
    def __init__(self, base_url: str, username: str, password: str):
        self.base = base_url.rstrip("/")
        self.s = requests.Session()
        self._login(username, password)

    def _login(self, username: str, password: str) -> None:
        r = self.s.get(f"{self.base}/api.php", params={
            "action": "query", "meta": "tokens", "type": "login", "format": "json"}, timeout=20)
        token = r.json()["query"]["tokens"]["logintoken"]
        r = self.s.post(f"{self.base}/api.php", data={
            "action": "login", "lgname": username, "lgpassword": password,
            "lgtoken": token, "format": "json"}, timeout=20)
        if r.json().get("login", {}).get("result") != "Success":
            raise RuntimeError("MediaWiki login failed")

    def edit(self, title: str, content: str) -> str:
        r = self.s.get(f"{self.base}/api.php", params={
            "action": "query", "meta": "tokens", "format": "json"}, timeout=20)
        csrf = r.json()["query"]["tokens"]["csrftoken"]
        self.s.post(f"{self.base}/api.php", data={
            "action": "edit", "title": title, "text": content,
            "token": csrf, "format": "json", "bot": 1}, timeout=20)
        return f"{self.base}/index.php/{title.replace(' ', '_')}"


def generate_docs(
    *,
    workspace_id,
    user_id,
    repo_root: Path,
    model: str,
    page_title: str | None = None,
) -> dict:
    """Analyse → render → (try to) publish. Returns a result dict.

    {"wikitext": str, "wiki_url": str|"", "published": bool, "files": int, "note": str}
    """
    structure = analyze_project(Path(repo_root))
    overview = _overview_via_llm(workspace_id, user_id, model, structure)
    wikitext = render_wikitext(structure, overview)
    title = page_title or structure["name"]

    wc = wiki_config()
    if not wc.get("enabled") or not wc.get("base_url") or not wc.get("password"):
        return {
            "wikitext": wikitext, "wiki_url": "", "published": False,
            "files": len(structure["files"]),
            "note": "MediaWiki not configured (local PC wiki disabled or missing password); "
                    "returning wikitext only.",
        }
    try:
        wiki = _Wiki(wc["base_url"], wc.get("username", "Angela"), wc["password"])
        url = wiki.edit(title, wikitext)
        return {"wikitext": wikitext, "wiki_url": url, "published": True,
                "files": len(structure["files"]), "note": "published to MediaWiki"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("docgen: publish failed: %s", exc)
        return {"wikitext": wikitext, "wiki_url": "", "published": False,
                "files": len(structure["files"]), "note": f"publish failed: {exc}"}
