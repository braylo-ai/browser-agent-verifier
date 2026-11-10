"""Local test web app (stdlib http.server + SQLite) with controllable faults.

Three flows:
  /contact                       one-page form            -> messages table
  /shop -> /cart -> /checkout -> /review -> /order/<id>   -> orders table
  /settings (fetch + toast)                               -> settings table

Faults are set per episode with POST /__plan (JSON):
  render_delay_ms      key buttons appear only after this delay
  swallow_first_click  first click on the submit button does nothing
  overlay_ms           a promo overlay intercepts clicks for this long
  submit               queue of outcomes for successive submits:
                       "ok" | "error" (503, nothing saved) |
                       "commit_then_error" (saved, then 503) |
                       "silent_fail" (success page/toast, nothing saved)

Read-only endpoints for an independent verifier: /api/messages, /api/orders,
/api/settings (JSON) and /admin/<table> (HTML).
"""

from __future__ import annotations

import html
import json
import sqlite3
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

ITEMS = {"mug": 12.0, "tee": 20.0, "cap": 15.0}
SCHEMA = """
CREATE TABLE messages (id INTEGER PRIMARY KEY, name TEXT, email TEXT, body TEXT);
CREATE TABLE orders (id INTEGER PRIMARY KEY, session TEXT, items TEXT, address TEXT, zip TEXT);
CREATE TABLE settings (user TEXT PRIMARY KEY, newsletter INTEGER);
"""


class State:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.lock = threading.Lock()
        self.reset({})

    def reset(self, plan: dict):
        with self.lock:
            self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self.conn.executescript(
                "DROP TABLE IF EXISTS messages; DROP TABLE IF EXISTS orders;"
                " DROP TABLE IF EXISTS settings;" + SCHEMA
            )
            self.conn.commit()
            self.plan = {
                "render_delay_ms": 0,
                "swallow_first_click": False,
                "overlay_ms": 0,
                "submit": [],
                **plan,
            }
            self.submit_queue = list(self.plan["submit"])
            self.carts: dict[str, list] = {}
            self.forms: dict[str, dict] = {}
            self.events: list[dict] = []

    def next_outcome(self) -> str:
        with self.lock:
            return self.submit_queue.pop(0) if self.submit_queue else "ok"

    def q(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur.fetchall()


def page(title: str, body: str, plan: dict, script: str = "") -> bytes:
    delay, swallow, overlay = (
        plan["render_delay_ms"],
        plan["swallow_first_click"],
        plan["overlay_ms"],
    )
    js = f"""
<script>
const DELAY={delay}, SWALLOW={str(swallow).lower()}, OVERLAY={overlay};
document.querySelectorAll('[data-late]').forEach(el => {{
  el.style.display='none'; setTimeout(() => el.style.display='', DELAY);
}});
if (SWALLOW) {{
  document.querySelectorAll('[data-submit]').forEach(el => {{
    let first = true;
    el.addEventListener('click', e => {{ if (first) {{ first=false; e.preventDefault(); e.stopImmediatePropagation(); }} }}, true);
  }});
}}
if (OVERLAY > 0) {{
  const o = document.createElement('div'); o.id='promo';
  o.style.cssText='position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:99';
  o.textContent='Sale! 20% off'; document.body.appendChild(o);
  setTimeout(() => o.remove(), OVERLAY);
}}
{script}
</script>"""
    return f"<!doctype html><html><head><meta charset=utf-8><title>{title}</title></head><body>{body}{js}</body></html>".encode()


def make_handler(state: State):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body: bytes, ctype="text/html", headers=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _redirect(self, loc, headers=None):
            self._send(303, b"", headers={"Location": loc, **(headers or {})})

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj).encode(), "application/json")

        def _session(self):
            c = SimpleCookie(self.headers.get("Cookie", ""))
            return c["sid"].value if "sid" in c else None

        def _form(self):
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n).decode()
            if self.headers.get("Content-Type", "").startswith("application/json"):
                return json.loads(raw or "{}")
            return {k: v[0] for k, v in parse_qs(raw).items()}

        def _error_page(self):
            self._send(
                503,
                page(
                    "Error",
                    "<h1>Service unavailable</h1><a href='/'>back</a>",
                    state.plan,
                ),
            )

        # ---------------- GET ----------------
        def do_GET(self):
            u = urlparse(self.path)
            p, qs = u.path, {k: v[0] for k, v in parse_qs(u.query).items()}
            plan = state.plan
            if p == "/contact":
                body = (
                    "<h1>Contact us</h1><form method=post action=/contact>"
                    "<input id=name name=name placeholder=Name><input id=email name=email placeholder=Email>"
                    "<textarea id=message name=message></textarea>"
                    "<button id=send type=submit data-late data-submit>Send</button></form>"
                )
                return self._send(200, page("Contact", body, plan))
            if p == "/thanks":
                return self._send(
                    200,
                    page("Thanks", "<h1 id=done>Thanks! Message received.</h1>", plan),
                )
            if p == "/shop":
                sid = self._session() or qs.get("sid")
                hdr = {"Set-Cookie": f"sid={sid}; Path=/"} if sid else {}
                items = "".join(
                    f"<form method=post action=/cart/add><input type=hidden name=sku value={k}>"
                    f"<button id=add-{k} data-late>Add {k} (${v:.2f})</button></form>"
                    for k, v in ITEMS.items()
                )
                return self._send(
                    200, page("Shop", f"<h1>Shop</h1>{items}", plan), headers=hdr
                )
            if p == "/cart":
                cart = state.carts.get(self._session(), [])
                lis = "".join(f"<li class=line>{html.escape(s)}</li>" for s in cart)
                return self._send(
                    200,
                    page(
                        "Cart",
                        f"<h1>Cart</h1><ul>{lis}</ul>"
                        "<a id=checkout href=/checkout data-late>Checkout</a>",
                        plan,
                    ),
                )
            if p == "/checkout":
                body = (
                    "<h1>Shipping</h1><form method=post action=/checkout>"
                    "<input id=address name=address><input id=zip name=zip>"
                    "<button id=continue data-late>Continue</button></form>"
                )
                return self._send(200, page("Checkout", body, plan))
            if p == "/review":
                f = state.forms.get(self._session(), {})
                body = (
                    f"<h1>Review</h1><p>{html.escape(f.get('address', ''))}</p>"
                    "<form method=post action=/order><button id=place data-late data-submit>Place order</button></form>"
                )
                return self._send(200, page("Review", body, plan))
            if p.startswith("/order/"):
                return self._send(
                    200,
                    page(
                        "Order",
                        f"<h1 id=done>Order #{html.escape(p[7:])} confirmed</h1>",
                        plan,
                    ),
                )
            if p == "/settings":
                body = (
                    "<h1>Settings</h1><label><input type=checkbox id=newsletter> Newsletter</label>"
                    "<button id=save data-late data-submit>Save</button><div id=toast></div>"
                )
                script = """
document.getElementById('save').addEventListener('click', async () => {
  const user = new URLSearchParams(location.search).get('user');
  const r = await fetch('/api/settings', {method:'POST', headers:{'Content-Type':'application/json'},
     body: JSON.stringify({user, newsletter: document.getElementById('newsletter').checked})});
  document.getElementById('toast').textContent = r.ok ? 'Saved!' : 'Error, try again';
});"""
                return self._send(200, page("Settings", body, plan, script))
            # ---- verifier endpoints (read-only) ----
            if p == "/api/messages":
                rows = state.q(
                    "SELECT name, email, body FROM messages WHERE email=?",
                    (qs.get("email"),),
                )
                return self._json(
                    [{"name": a, "email": b, "body": c} for a, b, c in rows]
                )
            if p == "/api/orders":
                rows = state.q(
                    "SELECT id, items, address, zip FROM orders WHERE session=?",
                    (qs.get("session"),),
                )
                return self._json(
                    [
                        {"id": i, "items": json.loads(it), "address": a, "zip": z}
                        for i, it, a, z in rows
                    ]
                )
            if p == "/api/settings":
                rows = state.q(
                    "SELECT newsletter FROM settings WHERE user=?", (qs.get("user"),)
                )
                return self._json(
                    {
                        "user": qs.get("user"),
                        "newsletter": bool(rows[0][0]) if rows else None,
                    }
                )
            if p.startswith("/admin/"):
                table = p[7:]
                if table not in ("messages", "orders", "settings"):
                    return self._send(404, b"no")
                rows = state.q(f"SELECT * FROM {table}")
                trs = "".join(
                    "<tr>"
                    + "".join(f"<td>{html.escape(str(c))}</td>" for c in r)
                    + "</tr>"
                    for r in rows
                )
                return self._send(200, f"<table id=rows>{trs}</table>".encode())
            self._send(404, b"not found")

        # ---------------- POST ----------------
        def do_POST(self):
            p = urlparse(self.path).path
            if p == "/__plan":
                state.reset(self._form())
                return self._json({"ok": True})
            f = self._form()
            sid = self._session()
            if p == "/cart/add":
                state.carts.setdefault(sid, []).append(f.get("sku"))
                return self._redirect("/cart")
            if p == "/checkout":
                state.forms[sid] = {
                    "address": f.get("address", ""),
                    "zip": f.get("zip", ""),
                }
                return self._redirect("/review")
            if p == "/contact":
                out = state.next_outcome()
                state.events.append({"submit": "contact", "outcome": out})
                if out == "error":
                    return self._error_page()
                if out != "silent_fail":
                    state.q(
                        "INSERT INTO messages (name, email, body) VALUES (?,?,?)",
                        (f.get("name"), f.get("email"), f.get("message")),
                    )
                if out == "commit_then_error":
                    return self._error_page()
                return self._redirect("/thanks")
            if p == "/order":
                out = state.next_outcome()
                state.events.append({"submit": "order", "outcome": out})
                if out == "error":
                    return self._error_page()
                oid = 0
                if out != "silent_fail":
                    cart, form = state.carts.get(sid, []), state.forms.get(sid, {})
                    items = {s: cart.count(s) for s in sorted(set(cart))}
                    state.q(
                        "INSERT INTO orders (session, items, address, zip) VALUES (?,?,?,?)",
                        (sid, json.dumps(items), form.get("address"), form.get("zip")),
                    )
                    oid = state.q("SELECT max(id) FROM orders")[0][0]
                if out == "commit_then_error":
                    return self._error_page()
                return self._redirect(f"/order/{oid or 1000}")
            if p == "/api/settings":
                out = state.next_outcome()
                state.events.append({"submit": "settings", "outcome": out})
                if out == "error":
                    return self._json({"ok": False}, 503)
                if out != "silent_fail":
                    state.q(
                        "INSERT OR REPLACE INTO settings (user, newsletter) VALUES (?,?)",
                        (f.get("user"), int(bool(f.get("newsletter")))),
                    )
                if out == "commit_then_error":
                    return self._json({"ok": False}, 503)
                return self._json({"ok": True})
            self._send(404, b"not found")

    return H


class App:
    """Run the app in a background thread on a free localhost port."""

    def __init__(self, db_path: str):
        self.state = State(db_path)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.state))
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
