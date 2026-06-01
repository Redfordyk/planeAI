"""Unit tests for Angela's pure helpers (no DB / no network).

Covers the parsing + safety logic that must not regress:
  - coder JSON extraction tolerates fences / prose
  - config sandbox guard rejects path traversal + unknown targets
  - docgen AST analysis + wikitext rendering
  - tester output summarisation
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from ai.angela import coder, config, docgen
from ai.angela.sandbox import CmdResult
from ai.angela.tester import _summarize


# --- coder JSON extraction -------------------------------------------------


def test_extract_json_plain():
    data = coder._extract_json('{"summary": "ok", "files": []}')
    assert data["summary"] == "ok"


def test_extract_json_fenced():
    raw = "```json\n{\"summary\": \"x\", \"files\": []}\n```"
    assert coder._extract_json(raw)["summary"] == "x"


def test_extract_json_with_prose():
    raw = "Sure! Here you go:\n{\"files\": [{\"path\": \"a.py\", \"content\": \"x\"}]}\nDone."
    data = coder._extract_json(raw)
    assert data["files"][0]["path"] == "a.py"


# --- config safety ---------------------------------------------------------


def test_resolve_unknown_target_raises():
    with pytest.raises(config.AngelaConfigError):
        config.resolve_target("definitely-not-a-real-target")


def test_assert_inside_sandbox_rejects_traversal():
    with pytest.raises(config.AngelaConfigError):
        config.assert_inside_sandbox("../../etc/passwd")


def test_assert_inside_sandbox_allows_child():
    p = config.assert_inside_sandbox("run-123")
    assert p.name == "run-123"


# --- docgen ----------------------------------------------------------------


def test_analyze_and_render(tmp_path: Path):
    (tmp_path / "mod.py").write_text(
        textwrap.dedent(
            '''
            class Greeter:
                """Says hi."""
                def hello(self, name):
                    """Greet by name."""
                    return name

            def top_level(x):
                """A function."""
                return x
            '''
        ),
        encoding="utf-8",
    )
    structure = docgen.analyze_project(tmp_path)
    assert structure["name"] == tmp_path.name
    assert len(structure["files"]) == 1
    f = structure["files"][0]
    assert f["classes"][0]["name"] == "Greeter"
    assert f["functions"][0]["name"] == "top_level"

    wt = docgen.render_wikitext(structure, overview="An overview.")
    assert "__TOC__" in wt
    assert "Greeter" in wt
    assert "top_level" in wt


def test_analyze_skips_files_without_defs(tmp_path: Path):
    (tmp_path / "data.py").write_text("X = 1\nY = 2\n", encoding="utf-8")
    structure = docgen.analyze_project(tmp_path)
    assert structure["files"] == []


# --- tester summary --------------------------------------------------------


def test_summarize_pytest_pass():
    res = CmdResult("pytest", 0, "===== 5 passed in 0.3s =====", "")
    assert "passed" in _summarize(res)


def test_summarize_timeout():
    res = CmdResult("pytest", 124, "", "timeout after 600s")
    assert _summarize(res) == "timeout"
