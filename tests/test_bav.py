"""Tests run against the real local app and real headless Chrome (system channel)."""

import json
import random
import tempfile
import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from bav.agent import POLICIES, run_task, task_plan
from bav.app import App
from bav.bench import params_for, sample_plan, set_plan, summarize
from bav.verifier import ground_truth, same_page_check, verify

P = params_for(7)


@pytest.fixture(scope="module")
def env():
    td = tempfile.mkdtemp()
    with App(str(Path(td) / "t.db")) as app, sync_playwright() as pw:
        b = pw.chromium.launch(channel="chrome", headless=True)
        yield app, b
        b.close()


def episode(env, task, plan, policy="robust"):
    app, b = env
    set_plan(app.url, plan)
    ctx = b.new_context()
    page = ctx.new_page()
    res = run_task(page, app.url, task, P, policy)
    same = same_page_check(page, task)
    ctx.close()
    return (
        res,
        same,
        verify(b, app.url, task, P),
        ground_truth(app.state.db_path, task, P),
    )


@pytest.mark.parametrize("task", ["contact", "checkout", "settings"])
def test_clean_run_completes_and_verifies(env, task):
    res, same, v, truth = episode(env, task, {"submit": ["ok"]})
    assert res.claimed_success and same and v.complete and truth


@pytest.mark.parametrize("task", ["contact", "checkout", "settings"])
def test_silent_fail_is_false_success_caught_by_verifier(env, task):
    res, same, v, truth = episode(env, task, {"submit": ["silent_fail"]})
    assert res.claimed_success and same  # agent and same-page check are fooled
    assert not truth and not v.complete  # independent verifier is not


@pytest.mark.parametrize("task", ["contact", "checkout"])
def test_commit_then_error_retry_duplicates(env, task):
    res, _, v, truth = episode(env, task, {"submit": ["commit_then_error", "ok"]})
    assert res.claimed_success and not truth
    assert "2" in v.reason and not v.complete


def test_settings_retry_is_idempotent(env):
    res, _, v, truth = episode(env, "settings", {"submit": ["commit_then_error", "ok"]})
    assert res.claimed_success and truth and v.complete


def test_error_without_retry_reports_failure(env):
    res, _, v, truth = episode(env, "contact", {"submit": ["error"]}, policy="none")
    assert not res.claimed_success and not truth and not v.complete


def test_robust_recovers_from_server_error(env):
    res, _, v, truth = episode(
        env, "contact", {"submit": ["error", "ok"]}, policy="robust"
    )
    assert res.claimed_success and truth and v.complete
    assert res.first_attempt_failures >= 1 and res.retries_used >= 1


def test_swallowed_click_needs_retry(env):
    res_none, *_ = episode(
        env, "contact", {"submit": ["ok"], "swallow_first_click": True}, policy="none"
    )
    res_naive, _, v, truth = episode(
        env, "contact", {"submit": ["ok"], "swallow_first_click": True}, policy="naive"
    )
    assert not res_none.claimed_success
    assert res_naive.claimed_success and truth and v.complete


def test_late_render_beats_short_timeout(env):
    plan = {"submit": ["ok"], "render_delay_ms": 3500}
    res_none, *_ = episode(env, "settings", plan, policy="none")
    res_rob, _, v, truth = episode(env, "settings", plan, policy="robust")
    assert not res_none.claimed_success
    assert res_rob.claimed_success and truth


def test_api_endpoints_are_read_only_views(env):
    app, _ = env
    set_plan(app.url, {})
    with urllib.request.urlopen(f"{app.url}/api/settings?user=nobody") as r:
        assert json.loads(r.read()) == {"user": "nobody", "newsletter": None}


def test_plan_reset_clears_db(env):
    episode(env, "contact", {"submit": ["ok"]})
    app, _ = env
    set_plan(app.url, {})
    assert not ground_truth(app.state.db_path, "contact", P)


def test_task_plans_have_expectations():
    for t in ("contact", "checkout", "settings"):
        segs = task_plan(t, P)
        assert segs and all("expect" in s and s["url"].startswith("/") for s in segs)


def test_sample_plan_deterministic_and_valid():
    a, b = sample_plan(random.Random("x")), sample_plan(random.Random("x"))
    assert a == b and a["submit"][0] in {
        "ok",
        "error",
        "commit_then_error",
        "silent_fail",
    }


def test_policies_defined():
    assert set(POLICIES) == {"none", "naive", "robust"}


def test_summarize_counts():
    rows = [
        {
            "task": "contact",
            "seed": 1,
            "policy": "none",
            "claimed": True,
            "same_page_ok": True,
            "verified": False,
            "verifier_reason": "api: 0 matching messages (want exactly 1)",
            "truth": False,
            "first_attempt_failures": 0,
            "retries_used": 0,
            "seconds": 1.0,
        },
        {
            "task": "contact",
            "seed": 2,
            "policy": "none",
            "claimed": True,
            "same_page_ok": True,
            "verified": True,
            "verifier_reason": "ok",
            "truth": True,
            "first_attempt_failures": 1,
            "retries_used": 1,
            "seconds": 1.0,
        },
    ]
    s = summarize(rows)["none/ALL"]
    assert s["false_success_claims"] == 1 and s["caught_by_verifier"] == 1
    assert (
        s["caught_by_same_page_check"] == 0
        and s["retry_recovered"] == 1
        and s["duplicates"] == 0
    )
