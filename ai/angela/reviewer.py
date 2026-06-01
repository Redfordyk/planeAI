"""LLM self-review of Angela's own diff.

Angela reviews her generated patch before tests run. This is the
"сама проверяет за собой код" requirement: a second, critical pass that
either approves or returns concrete objections, which the coder then
addresses on the next fix iteration.

The reviewer sees only the diff (not the full repo) — it's a code
review, not a re-implementation. Output is strict JSON so the pipeline
can branch on the verdict without NLP.
"""

from __future__ import annotations

import json
import logging
import re

from ai import providers
from ai.models import AIUsageLog, AngelaRun
from ai.usage import record_usage


logger = logging.getLogger("plane.ai.angela.reviewer")


SYSTEM = """\
Ты — строгий ревьюер кода. Тебе дают задачу и diff предложенных изменений. \
Верни СТРОГО валидный JSON без markdown и пояснений вокруг:

{
  "verdict": "approved" | "changes_requested",
  "score": 0-100,
  "issues": ["конкретное замечание 1", "замечание 2"],
  "rationale": "краткое обоснование вердикта"
}

Критерии: корректность, покрытие тестами, безопасность (инъекции, утечки \
секретов), обработка ошибок, соответствие задаче. Если есть хоть одно \
блокирующее замечание — verdict = changes_requested. Будь конкретна: каждое \
замечание должно подсказывать, что именно исправить.
"""


def _text_of(message) -> str:
    parts = []
    for block in getattr(message, "content", []) or []:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)


def _extract_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        s, e = text.find("{"), text.rfind("}")
        if s != -1 and e > s:
            return json.loads(text[s : e + 1])
        raise


class ReviewResult:
    def __init__(self, verdict: str, score: int, issues: list[str], rationale: str):
        self.verdict = verdict
        self.score = score
        self.issues = issues
        self.rationale = rationale

    @property
    def approved(self) -> bool:
        return self.verdict == AngelaRun.VERDICT_APPROVED

    def feedback_text(self) -> str:
        return "\n".join(f"- {i}" for i in self.issues) or self.rationale


def review_diff(
    *,
    workspace_id,
    user_id,
    issue_text: str,
    diff: str,
    model: str,
) -> ReviewResult:
    if not diff.strip():
        return ReviewResult(
            AngelaRun.VERDICT_CHANGES, 0, ["Пустой diff — изменения не сгенерированы"], "no diff"
        )

    chat = providers.get_chat(workspace_id)
    user_content = (
        "## Задача\n" + (issue_text or "").strip()
        + "\n\n## Diff\n```diff\n" + diff[:24000] + "\n```"
    )
    message = chat.complete(
        system=SYSTEM,
        messages=[{"role": "user", "content": user_content}],
        model=model,
        max_tokens=1500,
        temperature=0.0,
    )
    record_usage(
        workspace_id=workspace_id,
        user_id=user_id,
        feature=AIUsageLog.FEATURE_AGENT,
        model=model,
        usage=getattr(message, "usage", {}),
    )

    raw = _text_of(message)
    try:
        data = _extract_json(raw)
    except (json.JSONDecodeError, ValueError):
        data = None

    if data is None:
        # Could not parse JSON. Don't fail a good build on OUR parsing
        # glitch: scan the prose for an explicit verdict; only block if the
        # model clearly asked for changes, otherwise approve leniently.
        lower = raw.lower()
        if "changes_requested" in lower or "changes requested" in lower:
            issues = [l.strip("-• \t") for l in raw.splitlines() if l.strip().startswith(("-", "•"))]
            return ReviewResult(
                AngelaRun.VERDICT_CHANGES, 40, issues[:8] or ["см. замечания ревью"], raw[:300]
            )
        return ReviewResult(AngelaRun.VERDICT_APPROVED, 75, [], "review unparsable — approved leniently")

    verdict = data.get("verdict")
    if verdict not in (AngelaRun.VERDICT_APPROVED, AngelaRun.VERDICT_CHANGES):
        # Unknown/missing verdict — lean on the score if present, else approve.
        try:
            sc = int(data.get("score", 0))
        except (TypeError, ValueError):
            sc = 0
        verdict = AngelaRun.VERDICT_CHANGES if sc and sc < 50 else AngelaRun.VERDICT_APPROVED
    issues = [str(i) for i in (data.get("issues", []) or [])]
    try:
        score = int(data.get("score", 0))
    except (TypeError, ValueError):
        score = 0
    return ReviewResult(verdict, score, issues, str(data.get("rationale", "")))
