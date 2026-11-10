"""Benchmark: tasks x retry policies x seeded fault plans, real headless Chrome.

The same seed list is used for every policy (paired comparison). Each episode:
reset the app with a sampled fault plan -> agent runs -> record its claim ->
same-page check -> independent verifier -> SQLite ground truth.

Fault plans are SYNTHETIC (sampled from the distribution below); the browser,
HTTP server, DOM, timing and database are real.
"""

from __future__ import annotations

import argparse
import json
import random
import tempfile
import time
import urllib.request
from collections import defaultdict
from pathlib import Path

from playwright.sync_api import sync_playwright

from .agent import POLICIES, run_task
from .app import App
from .verifier import ground_truth, same_page_check, verify

TASKS = ["contact", "checkout", "settings"]


def sample_plan(rng: random.Random) -> dict:
    first = rng.choices(
        ["ok", "error", "commit_then_error", "silent_fail"], [0.55, 0.15, 0.15, 0.15]
    )[0]
    later = rng.choices(["ok", "error", "silent_fail"], [0.85, 0.10, 0.05], k=4)
    return {
        "render_delay_ms": rng.choice([0, 0, 800, 3500, 6000]),
        "swallow_first_click": rng.random() < 0.25,
        "overlay_ms": 3000 if rng.random() < 0.2 else 0,
        "submit": [first] + later,
    }


def params_for(seed: int) -> dict:
    return {
        "name": f"Test User {seed}",
        "email": f"user{seed}@example.test",
        "message": f"hello from run {seed}",
        "session": f"s{seed}",
        "address": f"{seed} Main St",
        "zip": f"{60600 + seed % 100}",
        "user": f"u{seed}",
    }


def set_plan(base: str, plan: dict):
    req = urllib.request.Request(
        base + "/__plan",
        data=json.dumps(plan).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=5).read()


def run(
    seeds: int, seed0: int = 1, tasks=TASKS, policies=tuple(POLICIES), headless=True
) -> list[dict]:
    rows = []
    with tempfile.TemporaryDirectory() as td, App(
        str(Path(td) / "app.db")
    ) as app, sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome", headless=headless)
        for task in tasks:
            for s in range(seed0, seed0 + seeds):
                plan = sample_plan(random.Random(f"{task}-{s}"))
                prm = params_for(s)
                for pol in policies:
                    set_plan(app.url, plan)
                    ctx = browser.new_context()
                    page = ctx.new_page()
                    t0 = time.monotonic()
                    res = run_task(page, app.url, task, prm, pol)
                    same = same_page_check(page, task)
                    ctx.close()
                    v = verify(browser, app.url, task, prm)
                    truth = ground_truth(app.state.db_path, task, prm)
                    rows.append(
                        {
                            "task": task,
                            "seed": s,
                            "policy": pol,
                            "plan": plan,
                            "claimed": res.claimed_success,
                            "same_page_ok": same,
                            "verified": v.complete,
                            "verifier_reason": v.reason,
                            "truth": truth,
                            "first_attempt_failures": res.first_attempt_failures,
                            "retries_used": res.retries_used,
                            "submit_outcomes": [e["outcome"] for e in app.state.events],
                            "seconds": round(time.monotonic() - t0, 2),
                        }
                    )
        browser.close()
    return rows


def summarize(rows: list[dict]) -> dict:
    out = {}
    groups = defaultdict(list)
    for r in rows:
        groups[(r["policy"], r["task"])].append(r)
        groups[(r["policy"], "ALL")].append(r)
    for (pol, task), rs in sorted(groups.items()):
        n = len(rs)
        flaky = [r for r in rs if r["first_attempt_failures"] > 0]
        false_claims = [r for r in rs if r["claimed"] and not r["truth"]]
        out[f"{pol}/{task}"] = {
            "episodes": n,
            "claimed_success": sum(r["claimed"] for r in rs),
            "true_success": sum(r["truth"] for r in rs),
            "flaky_episodes": len(flaky),
            "retry_recovered": sum(r["truth"] for r in flaky),
            "false_success_claims": len(false_claims),
            "caught_by_verifier": sum(not r["verified"] for r in false_claims),
            "caught_by_same_page_check": sum(
                not r["same_page_ok"] for r in false_claims
            ),
            "verifier_agrees_with_truth": sum(r["verified"] == r["truth"] for r in rs),
            "duplicates": sum(
                1
                for r in rs
                if "(want exactly 1)" in r["verifier_reason"]
                and not r["verifier_reason"].startswith("api: 0")
            ),
            "missed_claims_true": sum(1 for r in rs if not r["claimed"] and r["truth"]),
            "mean_seconds": round(sum(r["seconds"] for r in rs) / n, 2),
        }
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=20)
    ap.add_argument("--out", default="results")
    a = ap.parse_args(argv)
    t0 = time.time()
    rows = run(a.seeds)
    summ = summarize(rows)
    Path(a.out).mkdir(exist_ok=True)
    meta = {
        "data": "Real headless Chrome + real local app; fault plans are SYNTHETIC (seeded sampling)",
        "seeds_per_task": a.seeds,
        "episodes": len(rows),
        "wall_seconds": round(time.time() - t0, 1),
    }
    Path(a.out, "summary.json").write_text(
        json.dumps({**meta, "summary": summ}, indent=2) + "\n"
    )
    Path(a.out, "episodes.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows)
    )
    print(json.dumps({k: v for k, v in summ.items() if k.endswith("/ALL")}, indent=2))
    print(meta)


if __name__ == "__main__":
    main()
