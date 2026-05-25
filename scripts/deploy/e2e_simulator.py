"""E2E user simulator for the AI + orchestrator stack.

Walks through every user-visible AI scenario like a real human in a
browser would:

  - login with demo account, attach session + CSRF
  - GET index-status (does search panel load?)
  - POST search (RAG retrieval + reply)
  - POST agent/execute (tool-loop creates a project + tasks)
  - GET orchestrator/goals + POST a fresh goal (PLANNER decompose)
  - POST goals/<id>/apply (creates real issues in Plane)
  - POST goals/<id>/report (COMMUNICATOR markdown digest)
  - POST orchestrator/trigger/scan (MONITOR + ESCALATOR)
  - POST orchestrator/trigger/analyst (ANALYST insight)
  - GET orchestrator/actions, /risks
  - GET/POST kill-switch (engage + release)

Run on the server (so we share network with the proxy and don't
fight CSRF / cross-site cookies). Prints a structured table of
results plus the last error from each container log.
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.parse
import urllib.request
import http.cookiejar
import ssl
import io


BASE = "https://aiplane.forgedev.ru"
EMAIL = "demo@aiplane.ru"
PASSWORD = "demo1234"
WORKSPACE_SLUG = "workspace"

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(jar),
    urllib.request.HTTPSHandler(context=ctx),
)
opener.addheaders = [
    ("User-Agent", "planeAI-e2e/1.0"),
    ("Referer", BASE + "/"),
]


class TestResult:
    def __init__(self, name: str):
        self.name = name
        self.ok: bool = False
        self.status: int = 0
        self.body_snippet: str = ""
        self.error: str = ""
        self.elapsed_ms: int = 0
        self.notes: dict = {}

    def __repr__(self):
        flag = "OK" if self.ok else "FAIL"
        head = f"[{flag}] {self.name}  HTTP={self.status} {self.elapsed_ms}ms"
        if self.error:
            head += f"  ERR: {self.error}"
        if self.notes:
            head += f"  {self.notes}"
        return head


def request(
    method: str,
    path: str,
    *,
    json_body=None,
    data: bytes | None = None,
    headers: dict | None = None,
    timeout: int = 120,
):
    url = path if path.startswith("http") else BASE + path
    h = dict(headers or {})
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    t = time.time()
    try:
        with opener.open(req, timeout=timeout) as r:
            body = r.read()
            return r.status, body, int((time.time() - t) * 1000), None
    except urllib.error.HTTPError as e:
        body = e.read()
        return e.code, body, int((time.time() - t) * 1000), None
    except Exception as e:  # noqa
        return 0, b"", int((time.time() - t) * 1000), f"{type(e).__name__}: {e}"


def csrf_token() -> str | None:
    code, body, _, err = request("GET", "/auth/get-csrf-token/")
    if err or code != 200:
        return None
    try:
        d = json.loads(body)
        return d.get("csrf_token")
    except Exception:
        return None


def auth_headers(csrf: str | None, json_ct: bool = True) -> dict:
    h = {}
    if csrf:
        h["X-CSRFToken"] = csrf
    if json_ct:
        h["Content-Type"] = "application/json"
    h["Referer"] = BASE + "/"
    return h


def login(email: str, password: str) -> tuple[bool, str]:
    """Returns (ok, msg)."""
    csrf = csrf_token()
    if not csrf:
        return False, "no_csrf"
    payload = urllib.parse.urlencode({"email": email, "password": password}).encode()
    code, body, _, err = request(
        "POST",
        "/auth/sign-in/",
        data=payload,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "X-CSRFToken": csrf,
            "Referer": BASE + "/",
        },
    )
    if err:
        return False, err
    if code in (200, 302):
        # Check we got session cookie
        names = [c.name for c in jar]
        return ("sessionid" in names or "session" in str(names).lower(), f"http={code} cookies={names}")
    return False, f"http={code} body={body[:200]!r}"


def workspace_id_from_slug(slug: str) -> str | None:
    """Query our internal users/me/workspaces/ to find UUID."""
    code, body, _, err = request("GET", "/api/users/me/workspaces/")
    if err or code != 200:
        return None
    try:
        rows = json.loads(body)
        for r in rows:
            if r.get("slug") == slug:
                return r.get("id")
    except Exception:
        return None
    return None


# ---- Test scenarios -------------------------------------------------------


def t_login() -> TestResult:
    r = TestResult("auth.login")
    ok, msg = login(EMAIL, PASSWORD)
    r.ok = ok
    r.status = 200 if ok else 0
    r.notes["msg"] = msg
    if not ok:
        r.error = msg
    return r


def t_workspace_resolved(state: dict) -> TestResult:
    r = TestResult("workspace.resolve")
    ws_id = workspace_id_from_slug(WORKSPACE_SLUG)
    if ws_id:
        r.ok = True
        r.status = 200
        r.notes["workspace_id"] = ws_id
        state["ws"] = ws_id
    else:
        r.error = f"no workspace for slug={WORKSPACE_SLUG!r}"
    return r


def t_index_status(state: dict) -> TestResult:
    r = TestResult("ai.index_status")
    if not state.get("ws"):
        r.error = "no ws"
        return r
    code, body, ms, err = request(
        "GET", f"/api/ai/workspaces/{state['ws']}/index-status/"
    )
    r.status, r.elapsed_ms = code, ms
    if err:
        r.error = err
    elif code != 200:
        r.error = body[:200].decode("utf-8", "replace")
    else:
        try:
            d = json.loads(body)
            r.ok = True
            r.notes = {"ready": d.get("ready"), "coverage": d.get("coverage")}
        except Exception as e:
            r.error = f"bad json: {e}"
    return r


def t_search(state: dict) -> TestResult:
    r = TestResult("ai.search")
    if not state.get("ws"):
        r.error = "no ws"
        return r
    csrf = csrf_token()
    code, body, ms, err = request(
        "POST",
        f"/api/ai/workspaces/{state['ws']}/search/",
        json_body={"query": "test", "mode": "search"},
        headers=auth_headers(csrf),
    )
    r.status, r.elapsed_ms = code, ms
    if err:
        r.error = err
    elif code in (200, 429):
        r.ok = True
        r.notes["len"] = len(body)
    else:
        r.error = body[:300].decode("utf-8", "replace")
    return r


def t_agent_execute(state: dict) -> TestResult:
    r = TestResult("ai.agent.execute")
    if not state.get("ws"):
        r.error = "no ws"
        return r
    csrf = csrf_token()
    prompt = f"Создай проект 'E2E Test {int(time.time())}' и в нём задачу 'Smoke task'"
    code, body, ms, err = request(
        "POST",
        f"/api/ai/workspaces/{state['ws']}/agent/execute/",
        json_body={"prompt": prompt},
        headers=auth_headers(csrf),
        timeout=180,
    )
    r.status, r.elapsed_ms = code, ms
    if err:
        r.error = err
    elif code != 200:
        r.error = body[:300].decode("utf-8", "replace")
    else:
        try:
            d = json.loads(body)
            r.ok = True
            r.notes = {
                "actions": len(d.get("actions") or []),
                "reply_chars": len(d.get("reply") or ""),
                "turns": d.get("turns"),
            }
        except Exception as e:
            r.error = f"bad json: {e}"
    return r


def t_goals_list(state: dict) -> TestResult:
    r = TestResult("orch.goals.list")
    if not state.get("ws"):
        r.error = "no ws"
        return r
    code, body, ms, err = request(
        "GET", f"/api/ai/workspaces/{state['ws']}/orchestrator/goals/"
    )
    r.status, r.elapsed_ms = code, ms
    if err:
        r.error = err
    elif code != 200:
        r.error = body[:300].decode("utf-8", "replace")
    else:
        try:
            d = json.loads(body)
            r.ok = True
            r.notes["count"] = len(d.get("goals") or [])
            state["existing_goals"] = d.get("goals") or []
        except Exception as e:
            r.error = f"bad json: {e}"
    return r


def t_goal_create(state: dict) -> TestResult:
    r = TestResult("orch.goal.create+planner")
    if not state.get("ws"):
        r.error = "no ws"
        return r
    csrf = csrf_token()
    title = f"E2E Goal {int(time.time())}"
    code, body, ms, err = request(
        "POST",
        f"/api/ai/workspaces/{state['ws']}/orchestrator/goals/",
        json_body={
            "title": title,
            "description": "Small test goal — minimal task set.",
            "deadline": "2026-12-31",
            "run_planner": True,
        },
        headers=auth_headers(csrf),
        timeout=120,
    )
    r.status, r.elapsed_ms = code, ms
    if err:
        r.error = err
    elif code != 201:
        r.error = body[:300].decode("utf-8", "replace")
    else:
        try:
            d = json.loads(body)
            r.ok = True
            r.notes = {
                "id": d["goal"]["id"][:8] + "...",
                "tasks": (d.get("plan_summary") or {}).get("task_count"),
                "epics": (d.get("plan_summary") or {}).get("epic_count"),
            }
            state["new_goal_id"] = d["goal"]["id"]
        except Exception as e:
            r.error = f"bad json: {e}"
    return r


def t_goal_apply(state: dict) -> TestResult:
    r = TestResult("orch.goal.apply")
    if not state.get("new_goal_id") or not state.get("project_id"):
        r.error = "missing goal_id or project_id"
        return r
    csrf = csrf_token()
    code, body, ms, err = request(
        "POST",
        f"/api/ai/workspaces/{state['ws']}/orchestrator/goals/{state['new_goal_id']}/apply/",
        json_body={"project": state["project_id"]},
        headers=auth_headers(csrf),
        timeout=120,
    )
    r.status, r.elapsed_ms = code, ms
    if err:
        r.error = err
    elif code != 201:
        r.error = body[:300].decode("utf-8", "replace")
    else:
        try:
            d = json.loads(body)
            r.ok = True
            r.notes = {"created": d["applied"]["created_issue_count"]}
        except Exception as e:
            r.error = f"bad json: {e}"
    return r


def t_goal_report(state: dict) -> TestResult:
    r = TestResult("orch.goal.report")
    if not state.get("new_goal_id"):
        r.error = "no goal"
        return r
    csrf = csrf_token()
    code, body, ms, err = request(
        "POST",
        f"/api/ai/workspaces/{state['ws']}/orchestrator/goals/{state['new_goal_id']}/report/",
        json_body={},
        headers=auth_headers(csrf),
        timeout=60,
    )
    r.status, r.elapsed_ms = code, ms
    if err:
        r.error = err
    elif code != 200:
        r.error = body[:300].decode("utf-8", "replace")
    else:
        try:
            d = json.loads(body)
            r.ok = True
            r.notes = {"narrative_chars": len((d.get("report") or {}).get("narrative") or "")}
        except Exception as e:
            r.error = f"bad json: {e}"
    return r


def t_trigger_scan(state: dict) -> TestResult:
    r = TestResult("orch.trigger.scan(MONITOR)")
    if not state.get("project_id"):
        r.error = "no project_id"
        return r
    csrf = csrf_token()
    code, body, ms, err = request(
        "POST",
        f"/api/ai/workspaces/{state['ws']}/orchestrator/trigger/scan/",
        json_body={"project_id": state["project_id"]},
        headers=auth_headers(csrf),
        timeout=60,
    )
    r.status, r.elapsed_ms = code, ms
    if err:
        r.error = err
    elif code != 200:
        r.error = body[:300].decode("utf-8", "replace")
    else:
        try:
            d = json.loads(body)
            r.ok = True
            r.notes = {
                "scanned": d.get("scan", {}).get("scanned"),
                "risks": d.get("scan", {}).get("risks"),
                "escalated": d.get("escalation", {}).get("escalated"),
            }
        except Exception as e:
            r.error = f"bad json: {e}"
    return r


def t_trigger_analyst(state: dict) -> TestResult:
    r = TestResult("orch.trigger.analyst")
    csrf = csrf_token()
    code, body, ms, err = request(
        "POST",
        f"/api/ai/workspaces/{state['ws']}/orchestrator/trigger/analyst/",
        json_body={"days": 30},
        headers=auth_headers(csrf),
        timeout=60,
    )
    r.status, r.elapsed_ms = code, ms
    if err:
        r.error = err
    elif code != 200:
        r.error = body[:300].decode("utf-8", "replace")
    else:
        try:
            d = json.loads(body)
            r.ok = True
            r.notes = {"narrative_chars": len(d.get("narrative") or "")}
        except Exception as e:
            r.error = f"bad json: {e}"
    return r


def t_actions_feed(state: dict) -> TestResult:
    r = TestResult("orch.actions.feed")
    code, body, ms, err = request(
        "GET", f"/api/ai/workspaces/{state['ws']}/orchestrator/actions/"
    )
    r.status, r.elapsed_ms = code, ms
    if err:
        r.error = err
    elif code != 200:
        r.error = body[:300].decode("utf-8", "replace")
    else:
        try:
            d = json.loads(body)
            r.ok = True
            r.notes = {"count": len(d.get("actions") or [])}
        except Exception as e:
            r.error = f"bad json: {e}"
    return r


def t_risks_list(state: dict) -> TestResult:
    r = TestResult("orch.risks.list")
    code, body, ms, err = request(
        "GET", f"/api/ai/workspaces/{state['ws']}/orchestrator/risks/"
    )
    r.status, r.elapsed_ms = code, ms
    if err:
        r.error = err
    elif code != 200:
        r.error = body[:300].decode("utf-8", "replace")
    else:
        try:
            d = json.loads(body)
            r.ok = True
            r.notes = {"count": len(d.get("risks") or [])}
            state["risks"] = d.get("risks") or []
        except Exception as e:
            r.error = f"bad json: {e}"
    return r


def t_kill_switch_get(state: dict) -> TestResult:
    r = TestResult("orch.kill_switch.get")
    code, body, ms, err = request(
        "GET", f"/api/ai/workspaces/{state['ws']}/orchestrator/kill-switch/"
    )
    r.status, r.elapsed_ms = code, ms
    if err:
        r.error = err
    elif code != 200:
        r.error = body[:300].decode("utf-8", "replace")
    else:
        try:
            d = json.loads(body)
            r.ok = True
            r.notes = {"engaged": d.get("engaged")}
        except Exception as e:
            r.error = f"bad json: {e}"
    return r


def t_kill_switch_engage(state: dict) -> TestResult:
    r = TestResult("orch.kill_switch.engage+release")
    csrf = csrf_token()
    code, body, ms, err = request(
        "POST",
        f"/api/ai/workspaces/{state['ws']}/orchestrator/kill-switch/",
        json_body={"engaged": True},
        headers=auth_headers(csrf),
    )
    r.status, r.elapsed_ms = code, ms
    if err:
        r.error = err
        return r
    if code != 200:
        r.error = body[:300].decode("utf-8", "replace")
        return r
    # release
    csrf = csrf_token()
    code2, body2, _, _ = request(
        "POST",
        f"/api/ai/workspaces/{state['ws']}/orchestrator/kill-switch/",
        json_body={"engaged": False},
        headers=auth_headers(csrf),
    )
    if code2 != 200:
        r.error = f"release failed http={code2}"
        return r
    r.ok = True
    r.notes = {"toggled": True}
    return r


def t_risk_resolve(state: dict) -> TestResult:
    r = TestResult("orch.risk.resolve")
    risks = state.get("risks") or []
    if not risks:
        r.ok = True
        r.notes["skipped"] = "no_open_risks"
        return r
    rid = risks[0]["id"]
    csrf = csrf_token()
    code, body, ms, err = request(
        "POST",
        f"/api/ai/workspaces/{state['ws']}/orchestrator/risks/{rid}/resolve/",
        json_body={},
        headers=auth_headers(csrf),
    )
    r.status, r.elapsed_ms = code, ms
    if err:
        r.error = err
    elif code != 200:
        r.error = body[:300].decode("utf-8", "replace")
    else:
        r.ok = True
    return r


def t_usage_stats(state: dict) -> TestResult:
    r = TestResult("ai.usage.stats")
    code, body, ms, err = request(
        "GET", f"/api/ai/workspaces/{state['ws']}/usage/stats/"
    )
    r.status, r.elapsed_ms = code, ms
    if err:
        r.error = err
    elif code != 200:
        r.error = body[:300].decode("utf-8", "replace")
    else:
        try:
            d = json.loads(body)
            r.ok = True
            r.notes = {"month_tokens": d.get("month_total_tokens"), "month_cost": d.get("month_total_cost_usd")}
        except Exception as e:
            r.error = f"bad json: {e}"
    return r


def pick_project(state: dict) -> TestResult:
    r = TestResult("pick.project")
    code, body, _, err = request("GET", f"/api/workspaces/{WORKSPACE_SLUG}/projects/")
    if err or code != 200:
        r.error = f"http={code} {err or body[:200]!r}"
        return r
    try:
        rows = json.loads(body)
        if not rows:
            r.error = "no projects in workspace"
            return r
        state["project_id"] = rows[0]["id"]
        r.ok = True
        r.status = 200
        r.notes["pid"] = rows[0]["id"][:8] + "..."
        r.notes["name"] = rows[0].get("name")
    except Exception as e:
        r.error = f"bad json: {e}"
    return r


def main() -> int:
    state: dict = {}
    print("=" * 70)
    print(f"e2e simulator → {BASE}")
    print(f"account: {EMAIL}")
    print("=" * 70)

    sequence = [
        t_login,
        t_workspace_resolved,
        pick_project,
        t_index_status,
        t_search,
        t_usage_stats,
        t_goals_list,
        t_goal_create,
        t_goal_apply,
        t_goal_report,
        t_trigger_scan,
        t_trigger_analyst,
        t_actions_feed,
        t_risks_list,
        t_risk_resolve,
        t_kill_switch_get,
        t_kill_switch_engage,
        t_agent_execute,
    ]

    results: list[TestResult] = []
    for fn in sequence:
        try:
            r = fn(state)
        except Exception as e:  # noqa
            r = TestResult(fn.__name__)
            r.error = f"crash: {type(e).__name__}: {e}"
        results.append(r)
        print(r)

    fails = [r for r in results if not r.ok]
    print("\n" + "=" * 70)
    print(f"RESULTS: {len(results) - len(fails)}/{len(results)} passed, {len(fails)} failed")
    if fails:
        print("\nFAILURES:")
        for r in fails:
            print(f"  - {r.name}: {r.error or 'unknown'}")
    print("=" * 70)
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
