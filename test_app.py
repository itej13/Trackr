"""Self-check for the rules that matter: who can see what, the edit lock, drafts, late uploads, summaries.
Whisper and the LLM are faked, so no models are needed.   Run:  .venv/bin/python test_app.py"""
import json, os, tempfile, threading, urllib.error, urllib.request
from http.cookiejar import CookieJar

os.environ["TS_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
import app  # noqa: E402  (must come after TS_DB is set)

app.run_whisper = lambda path: "Rendered three Kling variants for the shoe client."
app.llm = lambda system, user: f"summary of {user.count(chr(10))} lines"
app.init_db()
app.create_user("boss", "Boss", "secret-one-1", is_admin=True)
emp = app.create_user("emp", "Emp", "secret-two-2", role="Video editor")
app.create_user("other", "Other", "secret-three-3")
srv = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_port}"
YESTERDAY_MS = (app.now() - 86400) * 1000


def client(username, password):
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))

    def call(method, path, body=None, headers=None, expect=200):
        data = body if isinstance(body, bytes) else None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(BASE + path, data, {"Content-Type": "application/json", **(headers or {})}, method=method)
        try:
            with opener.open(req) as r: code, out = r.status, json.load(r)
        except urllib.error.HTTPError as e: code, out = e.code, json.load(e)
        assert code == expect, f"{method} {path}: expected {expect}, got {code} {out}"
        return out
    call("POST", "/api/login", {"username": username, "password": password})
    return call


def drain():  # run the background jobs inline
    while not (app.whisper_jobs.q.empty() and app.llm_jobs.q.empty()):
        for jobs in (app.whisper_jobs, app.llm_jobs):
            while not jobs.q.empty():
                key = jobs.q.get(); jobs.queued.discard(key); jobs.fn(key)


def record(call, cid, at_ms=None):
    headers = {"Content-Type": "audio/webm", "X-Client-Id": cid}
    if at_ms: headers["X-Recorded-At"] = str(int(at_ms))
    return call("POST", "/api/entries", b"x" * 2000, headers)["id"]


# logins
try: client("emp", "wrong")
except AssertionError as e: assert "got 401" in str(e)
boss, e1, other = client("boss", "secret-one-1"), client("emp", "secret-two-2"), client("other", "secret-three-3")

# record -> draft. The employee sees it with a preview summary; the admin sees nothing yet.
eid = record(e1, "rec-0001")
assert record(e1, "rec-0001") == eid, "a retried upload must not create a second entry"
drain()
mine = e1("GET", "/api/day")
assert mine["entries"][0]["status"] == "draft" and mine["preview"] and mine["summary"]["text"]
assert boss("GET", f"/api/day?user={emp}")["entries"] == [], "admin must not see drafts"

# submit -> the admin sees the entry and the day summary; comments are admin-only
e1("PATCH", f"/api/entries/{eid}", {"transcript": "Fixed: three Kling variants."})
e1("POST", f"/api/entries/{eid}/submit")
drain()
seen = boss("GET", f"/api/day?user={emp}")
assert seen["entries"][0]["transcript"] == "Fixed: three Kling variants." and not seen["entries"][0]["edited"]
assert seen["summary"]["text"] and not seen["summary"]["stale"]
boss("POST", "/api/comments", {"user": emp, "text": "Nice work"})
assert boss("GET", f"/api/day?user={emp}")["comments"][0]["text"] == "Nice work"
assert "comments" not in e1("GET", "/api/day"), "employees never see comments"

# editing a submitted entry keeps the first version for the admin
e1("PATCH", f"/api/entries/{eid}", {"transcript": "Three Kling variants, client approved."})
seen = boss("GET", f"/api/day?user={emp}")["entries"][0]
assert seen["edited"] and seen["original"] == "Fixed: three Kling variants."

# access control
other("GET", f"/api/day?user={emp}", expect=403)
e1("GET", "/api/users", expect=403)
other("PATCH", f"/api/entries/{eid}", {"transcript": "hijack"}, expect=404)

# a recording made yesterday (offline) and uploaded today: goes straight in and is locked
late = record(e1, "rec-0002", YESTERDAY_MS)
drain()
day = app.ist_day(YESTERDAY_MS / 1000)
assert e1("GET", f"/api/day?date={day}")["entries"][0]["status"] == "submitted"
e1("PATCH", f"/api/entries/{late}", {"transcript": "change"}, expect=403)
e1("DELETE", f"/api/entries/{late}", expect=403)

# a draft left over from yesterday is kept as submitted
app.ex("INSERT INTO entries(user_id, client_id, day, recorded_at, status, transcript) VALUES(?,?,?,?,?,?)",
       emp, "rec-0003", day, YESTERDAY_MS / 1000, "draft", "forgot to submit")
assert all(e["status"] == "submitted" for e in e1("GET", f"/api/day?date={day}")["entries"])

# monthly summaries (per person and company) are generated on request
m = app.ist_day()[:7]
assert boss("GET", f"/api/month?user={emp}&m={m}")["summary"]["stale"]
boss("POST", "/api/summaries", {"user": emp, "month": m})
boss("POST", "/api/summaries", {"month": m})
drain()
assert boss("GET", f"/api/month?user={emp}&m={m}")["summary"]["text"]
assert boss("GET", f"/api/company?m={m}")["summary"]["text"]
assert "summary" not in e1("GET", f"/api/month?m={m}"), "monthly summaries are admin-only"

# deactivating someone logs them out
boss("PATCH", f"/api/users/{emp}", {"active": False})
e1("GET", "/api/me", expect=401)
boss("PATCH", "/api/users/1", {"active": False}, expect=400)  # can't lock yourself out

# internet-facing basics: health check, security headers, password length, login throttle
with urllib.request.urlopen(BASE + "/api/health") as r: assert json.load(r)["ok"]
with urllib.request.urlopen(BASE + "/") as r: assert "default-src 'self'" in r.headers["Content-Security-Policy"]
boss("POST", "/api/users", {"username": "shorty", "name": "Shorty", "password": "short-pw"}, expect=400)
for _ in range(5):
    try: client("other", "wrong-password")
    except AssertionError: pass
try: client("other", "secret-three-3"); raise SystemExit("6th login after 5 failures should be refused")
except AssertionError as e: assert "got 429" in str(e)

print("all checks passed")
