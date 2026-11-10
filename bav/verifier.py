"""Independent completion verifier.

It never looks at the agent's page or its claim. It checks the app's real
state through two channels the agent did not use:
  * API  - read-only JSON endpoints (/api/messages, /api/orders, /api/settings)
  * DOM  - a FRESH browser context loading /admin/<table>
and requires the exact expected effect, exactly once (a duplicate order or
message is a failure too).

`ground_truth()` reads SQLite directly; the benchmark uses it to score the
verifier and the agent. The verifier itself does not touch the database file.

`same_page_check()` is the weak baseline: re-read the agent's own page for
the success text (what a naive "screenshot judge" does).
"""

from __future__ import annotations

import json
import sqlite3
import urllib.request
from dataclasses import dataclass

from playwright.sync_api import Browser, Page


@dataclass
class Verdict:
    complete: bool
    reason: str


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read())


def expected(task: str, p: dict):
    return {
        "contact": {"name": p["name"], "email": p["email"], "body": p["message"]},
        "checkout": {"items": {"mug": 1}, "address": p["address"], "zip": p["zip"]},
        "settings": {"newsletter": True},
    }[task]


def _admin_rows(browser: Browser, base: str, table: str) -> int:
    ctx = browser.new_context()
    try:
        pg = ctx.new_page()
        pg.goto(f"{base}/admin/{table}")
        return pg.locator("#rows tr").count()
    finally:
        ctx.close()


def verify(browser: Browser, base: str, task: str, p: dict) -> Verdict:
    exp = expected(task, p)
    if task == "contact":
        rows = _get(f"{base}/api/messages?email={p['email']}")
        dom = _admin_rows(browser, base, "messages")
        if len(rows) != 1:
            return Verdict(
                False, f"api: {len(rows)} matching messages (want exactly 1)"
            )
        if rows[0] != exp:
            return Verdict(False, f"api: message differs {rows[0]}")
        if dom != 1:
            return Verdict(False, f"dom: admin table has {dom} rows")
        return Verdict(True, "api+dom: exactly one matching message")
    if task == "checkout":
        rows = _get(f"{base}/api/orders?session={p['session']}")
        dom = _admin_rows(browser, base, "orders")
        if len(rows) != 1:
            return Verdict(False, f"api: {len(rows)} orders (want exactly 1)")
        o = rows[0]
        got = {"items": o["items"], "address": o["address"], "zip": o["zip"]}
        if got != exp:
            return Verdict(False, f"api: order differs {got}")
        if dom != 1:
            return Verdict(False, f"dom: admin table has {dom} rows")
        return Verdict(True, "api+dom: exactly one correct order")
    if task == "settings":
        s = _get(f"{base}/api/settings?user={p['user']}")
        if s["newsletter"] is not True:
            return Verdict(False, f"api: newsletter={s['newsletter']}")
        if _admin_rows(browser, base, "settings") != 1:
            return Verdict(False, "dom: settings row missing")
        return Verdict(True, "api+dom: newsletter on")
    raise ValueError(task)


def same_page_check(page: Page, task: str) -> bool:
    try:
        if task == "contact":
            return "/thanks" in page.url
        if task == "checkout":
            return "confirmed" in (
                page.locator("#done").first.text_content(timeout=500) or ""
            )
        if task == "settings":
            return "Saved!" in (
                page.locator("#toast").first.text_content(timeout=500) or ""
            )
    except Exception:
        return False
    return False


def ground_truth(db_path: str, task: str, p: dict) -> bool:
    exp = expected(task, p)
    con = sqlite3.connect(db_path)
    try:
        if task == "contact":
            rows = con.execute("SELECT name, email, body FROM messages").fetchall()
            return rows == [(exp["name"], exp["email"], exp["body"])]
        if task == "checkout":
            rows = con.execute(
                "SELECT session, items, address, zip FROM orders"
            ).fetchall()
            return (
                len(rows) == 1
                and rows[0][0] == p["session"]
                and json.loads(rows[0][1]) == exp["items"]
                and rows[0][2] == exp["address"]
                and rows[0][3] == exp["zip"]
            )
        if task == "settings":
            return con.execute("SELECT user, newsletter FROM settings").fetchall() == [
                (p["user"], 1)
            ]
    finally:
        con.close()
    raise ValueError(task)
