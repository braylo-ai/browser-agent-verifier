"""Scripted Playwright browser agent with pluggable retry policies.

No LLM: each task is a fixed plan of segments (start URL, actions, success
expectation). This keeps the browser mechanics real and the runs
reproducible; the plan is what an LLM planner would emit.

The agent CLAIMS success when every segment's expectation is met in the page
it is looking at (URL, heading or toast). It never checks the database: that
is the verifier's job.

Retry policies:
  none     2.5 s action timeout, no retries
  naive    2.5 s timeout, retry the failing action up to 3x in place, 300 ms apart
  robust   5 s timeout (doubles per retry), on failure reload the segment's start URL and redo the
           whole segment, exponential backoff 250 ms * 2^k, up to 3 retries
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from playwright.sync_api import Error as PWError
from playwright.sync_api import Page

POLICIES = {
    "none": {"timeout": 2500, "retries": 0, "mode": "in_place", "backoff": 0},
    "naive": {"timeout": 2500, "retries": 3, "mode": "in_place", "backoff": 300},
    "robust": {"timeout": 5000, "retries": 3, "mode": "segment", "backoff": 250},
}


def task_plan(task: str, p: dict) -> list[dict]:
    """p holds the episode's parameters (unique email / session / user)."""
    if task == "contact":
        return [
            {
                "url": "/contact",
                "actions": [
                    ("fill", "#name", p["name"]),
                    ("fill", "#email", p["email"]),
                    ("fill", "#message", p["message"]),
                    ("click", "#send"),
                ],
                "expect": ("url", "/thanks"),
            }
        ]
    if task == "checkout":
        return [
            {
                "url": f"/shop?sid={p['session']}",
                "actions": [("click", "#add-mug")],
                "expect": ("url", "/cart"),
            },
            {
                "url": "/cart",
                "actions": [("click", "#checkout")],
                "expect": ("url", "/checkout"),
            },
            {
                "url": "/checkout",
                "actions": [
                    ("fill", "#address", p["address"]),
                    ("fill", "#zip", p["zip"]),
                    ("click", "#continue"),
                ],
                "expect": ("url", "/review"),
            },
            {
                "url": "/review",
                "actions": [("click", "#place")],
                "expect": ("text", "#done", "confirmed"),
            },
        ]
    if task == "settings":
        return [
            {
                "url": f"/settings?user={p['user']}",
                "actions": [("check", "#newsletter"), ("click", "#save")],
                "expect": ("text", "#toast", "Saved!"),
            }
        ]
    raise ValueError(task)


@dataclass
class AgentResult:
    claimed_success: bool
    attempts: list[dict] = field(default_factory=list)  # every action/expect attempt
    final_url: str = ""
    seconds: float = 0.0

    @property
    def first_attempt_failures(self) -> int:
        return sum(1 for a in self.attempts if a["try"] == 0 and not a["ok"])

    @property
    def retries_used(self) -> int:
        return sum(1 for a in self.attempts if a["try"] > 0)


def _do(page: Page, act, timeout):
    kind, sel = act[0], act[1]
    loc = page.locator(sel)
    if kind == "fill":
        loc.fill(act[2], timeout=timeout)
    elif kind == "click":
        loc.click(timeout=timeout)
    elif kind == "check":
        loc.check(timeout=timeout)


def _expect(page: Page, exp, timeout) -> bool:
    deadline = time.monotonic() + timeout / 1000
    while time.monotonic() < deadline:
        try:
            if exp[0] == "url" and exp[1] in page.url:
                return True
            if exp[0] == "text":
                loc = page.locator(exp[1])
                if loc.count() and exp[2] in (loc.first.text_content() or ""):
                    return True
        except PWError:
            pass  # page navigating
        page.wait_for_timeout(50)
    return False


def run_task(
    page: Page, base: str, task: str, params: dict, policy: str
) -> AgentResult:
    cfg = POLICIES[policy]
    res = AgentResult(False)
    t0 = time.monotonic()
    for si, seg in enumerate(task_plan(task, params)):
        steps = list(seg["actions"]) + [("expect", seg["expect"])]
        ok_seg = False
        for attempt in range(cfg["retries"] + 1):
            if attempt and cfg["backoff"]:
                page.wait_for_timeout(
                    cfg["backoff"]
                    * (2 ** (attempt - 1) if cfg["mode"] == "segment" else 1)
                )
            if attempt == 0 or cfg["mode"] == "segment":
                start = 0
                try:
                    page.goto(base + seg["url"])
                except PWError as e:
                    # e.g. an earlier submit is still navigating; count it as a failed try
                    res.attempts.append(
                        {
                            "segment": si,
                            "step": -1,
                            "op": "goto",
                            "try": attempt,
                            "ok": False,
                            "error": str(e).splitlines()[0][:160],
                        }
                    )
                    continue
            # in_place mode resumes at the step that failed
            tmo = cfg["timeout"] * (2 ** attempt if cfg["mode"] == "segment" else 1)
            failed_at = None
            for i in range(start, len(steps)):
                st = steps[i]
                try:
                    if st[0] == "expect":
                        ok = _expect(page, st[1], tmo)
                    else:
                        _do(page, st, tmo)
                        ok = True
                except PWError as e:
                    ok, err = False, str(e).splitlines()[0][:160]
                else:
                    err = None
                rec = {"segment": si, "step": i, "op": st[0], "try": attempt, "ok": ok}
                if err:
                    rec["error"] = err
                res.attempts.append(rec)
                if not ok:
                    failed_at = i
                    break
            if failed_at is None:
                ok_seg = True
                break
            # naive in-place: if the expectation failed, the submit click is retried
            start = (
                failed_at if steps[failed_at][0] != "expect" else max(0, failed_at - 1)
            )
        if not ok_seg:
            res.final_url, res.seconds = page.url, time.monotonic() - t0
            return res
    res.claimed_success = True
    res.final_url, res.seconds = page.url, time.monotonic() - t0
    return res
