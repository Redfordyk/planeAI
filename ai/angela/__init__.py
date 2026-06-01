"""Angela — autonomous coding agent (sandbox-scoped).

Angela takes a Plane Issue (or a freeform prompt) and runs a bounded
``code → self-review → test → deploy`` loop against an ISOLATED
sandbox / demo repository. She never touches the user's production
repo and never modifies this planeAI codebase — the only writable
target is an allow-listed sandbox checkout under ``ANGELA_WORKDIR``.

Module map:

    config.py     — settings-driven sandbox registry + safety guards
    base.py       — step-logging helper used by every phase
    sandbox.py    — clone / branch / apply-patch / run-cmd in isolation
    coder.py      — LLM code generation for an issue (autocode lineage)
    reviewer.py   — LLM self-review of the generated diff
    tester.py     — run the sandbox repo's test command, parse result
    deployer.py   — 3 deploy strategies (staging+gate / autonomous / manual)
    docgen.py     — AST project analysis → docs → (local) MediaWiki
    pipeline.py   — orchestrates the whole run, persists AngelaRun/Step
    api.py        — DRF endpoints (wired in ai/urls.py)

Safety invariants (mirror CLAUDE.md):
  1. Sandbox isolation — every filesystem op is asserted to live under
     ``ANGELA_WORKDIR``; a client never supplies a clone URL.
  2. ACL in code — the *view* checks workspace membership + AI budget
     before any pipeline runs; the LLM only proposes, our code applies.
  3. Untrusted content — issue text goes only into the ``user`` role of
     LLM calls; instructions live in the ``system`` role.
  4. Token budget — every LLM round-trip funnels through
     ``ai.usage.record_usage`` under ``FEATURE_AGENT``.
"""

from __future__ import annotations
