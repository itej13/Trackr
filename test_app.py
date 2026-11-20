"""Self-check for the rules that matter: who can see what, the edit lock, drafts, late uploads, summaries.
Whisper and the LLM are faked, so no models are needed.   Run:  .venv/bin/python test_app.py"""
import base64, json, os, re, sqlite3, subprocess, sys, tempfile, threading, urllib.error, urllib.request
from http.cookiejar import CookieJar
from pathlib import Path

os.environ["TS_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
import app  # noqa: E402  (must come after TS_DB is set)

app.run_whisper = lambda path: "Rendered three Kling variants for the shoe client."
TASK_REPLY, task_calls = ["none"], []  # what the fake model answers to the "task done?" check, and what it was asked
summary_calls = []


def fake_llm(system, user):
    if system == app.TASK_CHECK:
        task_calls.append(user)
        return TASK_REPLY[0]
    summary_calls.append(user)
    return f"summary of {user.count(chr(10))} lines"


app.llm = fake_llm
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
    while not (app.whisper_jobs.q.empty() and app.llm_jobs.q.empty() and app.slack_jobs.q.empty()):
        for jobs in (app.whisper_jobs, app.llm_jobs, app.slack_jobs):
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
drain()
before = len(summary_calls)
e1("POST", f"/api/entries/{eid}/submit")
drain()
assert len(summary_calls) == before, "submitting unchanged notes must reuse the preview, not call the model again"
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

# speaking guide: on your own Today screen only; admins change it, empty goes back to the default
assert e1("GET", "/api/day")["guide"] == app.DEFAULT_GUIDE
assert "guide" not in boss("GET", f"/api/day?user={emp}") and "guide" not in e1("GET", "/api/day?date=2026-01-01")
e1("PUT", "/api/guide", {"text": "hijack"}, expect=403)
boss("PUT", "/api/guide", {"text": "Say it brand by brand."})
assert e1("GET", "/api/day")["guide"] == "Say it brand by brand." == boss("GET", "/api/glossary")["guide"]
boss("PUT", "/api/guide", {"text": "  "})
assert e1("GET", "/api/day")["guide"] == app.DEFAULT_GUIDE

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

# reports: any date range (max 92 days), one person or everyone; admin only
e1("GET", "/api/range", expect=403)
wk = boss("GET", "/api/range")
assert app.datetime.strptime(wk["from"], "%Y-%m-%d").weekday() == 0 and app.span("range", f"{wk['from']}..{wk['to']}")[1] == wk["to"]
boss("GET", "/api/range?from=2026-01-01&to=2026-04-30", expect=400)  # 120 days
boss("GET", "/api/range?from=2026-02-02&to=2026-02-01", expect=400)
today = app.ist_day()
r = boss("GET", f"/api/range?user={emp}&from={day}&to={today}")
assert set(r["days"]) == {day, today} and r["days"][today]["entries"][0]["transcript"].startswith("Three Kling")
assert r["summary"]["stale"] and not r["summary"]["text"], "range summaries are only written on request"
assert {p["id"]: p["days"] for p in boss("GET", f"/api/range?from={day}&to={today}")["people"]}[emp] == {day: 2, today: 1}
boss("POST", "/api/summaries", {"user": emp, "from": day, "to": today})
boss("POST", "/api/summaries", {"from": day, "to": today})
boss("POST", "/api/summaries", {"from": today, "to": day}, expect=400)
drain()
assert boss("GET", f"/api/range?user={emp}&from={day}&to={today}")["summary"]["text"]
assert boss("GET", f"/api/range?from={day}&to={today}")["summary"]["text"]
assert "month" not in app.PROMPTS["range"].lower() + app.PROMPTS["range_all"].lower()

# tasks: admins assign; employees see only their own and change only the status
assert task_calls == [], "notes so far were recorded with no open tasks: no model call"
yesterday, tomorrow = app.ist_day(app.now() - 86400), app.ist_day(app.now() + 86400)
oth = app.q1("SELECT id FROM users WHERE username='other'")["id"]
t_late = boss("POST", "/api/tasks", {"title": "Shoe reel CTA", "assignee": emp, "due": yesterday, "client": "Shoe brand"})["id"]
t_next = boss("POST", "/api/tasks", {"title": "Skincare script", "assignee": emp, "due": tomorrow})["id"]
t_done = boss("POST", "/api/tasks", {"title": "Old thing", "assignee": emp, "due": yesterday, "status": "done"})["id"]
t_oth = boss("POST", "/api/tasks", {"title": "Not yours", "assignee": oth})["id"]
boss("POST", "/api/tasks", {"title": "No person"}, expect=400)
e1("POST", "/api/tasks", {"title": "Self-assigned", "assignee": emp}, expect=403)
assert {t["id"] for t in e1("GET", f"/api/tasks?user={oth}")["tasks"]} == {t_late, t_next, t_done}, "only your own tasks"
assert {t["id"] for t in e1("GET", "/api/day")["tasks"]} == {t_late, t_next, t_done}
other("PATCH", f"/api/tasks/{t_late}", {"status": "done"}, expect=404)
for change in ({"title": "x"}, {"assignee": oth}, {"due": tomorrow}, {"details": "x"}, {"status": "cancelled"}):
    e1("PATCH", f"/api/tasks/{t_late}", change, expect=403)
e1("PATCH", f"/api/tasks/{t_next}", {"status": "in_progress"})
assert [t["id"] for t in boss("GET", "/api/tasks?overdue=1")["tasks"]] == [t_late], "overdue: open and past due only"
assert {t["id"] for t in boss("GET", f"/api/tasks?user={emp}&status=open")["tasks"]} == {t_late, t_next}

# AI "task done?" check: only the person's own open tasks, strict parsing, nothing changes until they say so
TASK_REPLY[0] = f"T{t_late}, T{t_oth}, T{t_done}, T999"  # someone else's, already done, and an invented ID
note = record(e1, "rec-0005")
drain()
assert f"T{t_late}: Shoe reel CTA" in task_calls[-1] and f"T{t_oth}" not in task_calls[-1]
assert [(s["task_id"], s["title"]) for s in e1("GET", "/api/day")["suggestions"]] == [(t_late, "Shoe reel CTA")]
assert app.q1("SELECT status FROM tasks WHERE id=?", t_late)["status"] == "todo", "the model never changes a task"
other("POST", f"/api/suggestions/{note}/{t_late}/accept", expect=404)
e1("POST", f"/api/suggestions/{note}/{t_late}/accept")
assert app.q1("SELECT status FROM tasks WHERE id=?", t_late)["status"] == "done" and not e1("GET", "/api/day")["suggestions"]
TASK_REPLY[0] = f"T{t_next} is finished"  # chatty answers are ignored, not mined for numbers
record(e1, "rec-0006"); drain()
assert not e1("GET", "/api/day")["suggestions"]
TASK_REPLY[0] = f"T{t_next}"
n2 = record(e1, "rec-0007"); drain()
e1("POST", f"/api/suggestions/{n2}/{t_next}/dismiss")
assert app.q1("SELECT status FROM tasks WHERE id=?", t_next)["status"] == "in_progress" and not e1("GET", "/api/day")["suggestions"]
app.run_whisper = lambda path: "Worked on thumbnails. Finished the logo Next the skincare script"  # speech: full stops may be missing
record(e1, "rec-0010"); drain()
assert "thumbnails" in task_calls[-1] and "logo" in task_calls[-1] and "Next" not in task_calls[-1], "plans are dropped first"
app.run_whisper = lambda path: "Rendered three Kling variants for the shoe client."
calls = len(task_calls)
record(other, "rec-0008"); drain()  # "other" has an open task, so one call; then none once it's closed
boss("PATCH", f"/api/tasks/{t_oth}", {"status": "cancelled"})
record(other, "rec-0009"); drain()
assert len(task_calls) == calls + 1, "no model call when the person has no open tasks"
other("PATCH", f"/api/tasks/{t_oth}", {"status": "todo"}, expect=403)  # an admin cancelled it: only an admin reopens it

# CSRF: a write from another site's page is refused (sslip.io isn't a public suffix); our own pages and non-browsers pass
boss("PUT", "/api/guide", {"text": ""}, {"Origin": "http://evil.sslip.io"}, expect=403)
boss("PUT", "/api/guide", {"text": ""}, {"Origin": BASE})
boss("PUT", "/api/guide", {"text": ""})
boss("POST", "/api/entries", b"", {"Content-Length": "-1", "X-Client-Id": "rec-neg-len"}, expect=400)

# deactivating someone logs them out
boss("PATCH", f"/api/users/{emp}", {"active": False})
e1("GET", "/api/me", expect=401)
boss("PATCH", "/api/users/1", {"active": False}, expect=400)  # can't lock yourself out
# their tasks stay visible to admins, flagged, and can be reassigned; nothing new can be given to them
boss("POST", "/api/tasks", {"title": "Too late", "assignee": emp}, expect=400)
assert {t["assignee_active"] for t in boss("GET", f"/api/tasks?user={emp}")["tasks"]} == {False}
boss("PATCH", f"/api/tasks/{t_next}", {"assignee": emp}, expect=400)
boss("PATCH", f"/api/tasks/{t_next}", {"assignee": oth, "title": "Skincare script v2"})
assert app.q1("SELECT assignee_id, title FROM tasks WHERE id=?", t_next)[:] == (oth, "Skincare script v2")

# internet-facing basics: health check, security headers, password length, login throttle
with urllib.request.urlopen(BASE + "/api/health") as r: assert json.load(r)["ok"]
with urllib.request.urlopen(BASE + "/") as r: assert "default-src 'self'" in r.headers["Content-Security-Policy"]
boss("POST", "/api/users", {"username": "shorty", "name": "Shorty", "password": "short-pw"}, expect=400)
for _ in range(5):
    try: client("other", "wrong-password")
    except AssertionError: pass
try: client("other", "secret-three-3"); raise SystemExit("6th login after 5 failures should be refused")
except AssertionError as e: assert "got 429" in str(e)

# work email: optional, unique, case-insensitive
boss("POST", "/api/users", {"username": "mailer", "name": "Mailer", "password": "secret-four-4", "email": "Riya@Example.com"})
boss("POST", "/api/users", {"username": "mailer2", "name": "M2", "password": "secret-four-4", "email": "riya@example.com"}, expect=409)
boss("POST", "/api/users", {"username": "mailer3", "name": "M3", "password": "secret-four-4", "email": "not-an-email"}, expect=400)
boss("PATCH", f"/api/users/{emp}", {"email": "RIYA@example.com"}, expect=409)
assert {u["username"]: u["email"] for u in boss("GET", "/api/users")["users"]}["mailer"] == "riya@example.com"

# ---------- Google Sheet sync, against an in-memory stand-in for the five Sheets API calls it makes ----------
class FakeSheet:
    def __init__(self): self.grids, self.requests = {}, 0

    def tabs(self): self.requests += 1; return {t: i for i, t in enumerate(self.grids)}
    def add_tabs(self, titles): self.requests += 1; self.grids.update({t: [] for t in titles})

    def read(self, ranges):  # like Google: no trailing empty cells or rows
        self.requests += 1
        out = []
        for r in ranges:
            tab, c1, r1, c2, r2 = re.fullmatch(r"'(.+)'!([A-Z])(\d*):([A-Z])(\d*)", r).groups()
            rows = [row[ord(c1) - 65:ord(c2) - 64] for row in self.grids[tab][int(r1 or 1) - 1:int(r2) if r2 else None]]
            rows = [row[:max((i + 1 for i, v in enumerate(row) if v not in ("", None)), default=0)] for row in rows]
            while rows and not rows[-1]: rows.pop()
            out.append(rows)
        return out

    def write(self, data):
        self.requests += 1
        for rng, rows in data:
            tab, c, r = re.fullmatch(r"'(.+)'!([A-Z])(\d+)", rng).groups()
            for i, row in enumerate(rows):
                g, n, c0 = self.grids[tab], int(r) - 1 + i, ord(c) - 65
                while len(g) <= n: g.append([])
                g[n] = g[n] + [""] * (c0 + len(row) - len(g[n]))
                g[n][c0:c0 + len(row)] = row

    def append(self, tab, rows):
        self.requests += 1
        g = self.grids[tab]
        while g and not any(v not in ("", None) for v in g[-1]): g.pop()
        g.extend(list(r) for r in rows)

    def delete_rows(self, rows):
        self.requests += 1
        for sid, n in sorted(rows, key=lambda r: -r[1]): del self.grids[list(self.grids)[sid]][n - 1]


fs = FakeSheet()
sync = lambda: app.sync_sheet(fs)
grid = lambda name: [[app.cell(v) for v in row] for row in fs.grids[name]]
row_of = lambda name, col, value: next(i for i, r in enumerate(grid(name)) if len(r) > col and r[col] == value)
user = lambda login: app.q1("SELECT * FROM users WHERE username=?", login)


def put(name, login, col, value, key_col=1):
    r = fs.grids[name][row_of(name, key_col, login)]
    r += [""] * (col + 1 - len(r))
    r[col] = value


# the first sync builds all four tabs from the app; a second one with nothing changed writes nothing
assert sync()[1] == []
assert grid("People")[0] == app.PEOPLE_COLS and len(grid("People")) == 1 + app.q1("SELECT COUNT(*) FROM users")[0]
assert grid("People")[row_of("People", 1, "boss")][4:6] == ["Yes", "Yes"]
assert len(grid("Entries")) == 1 + app.q1("SELECT COUNT(*) FROM entries WHERE status='submitted'")[0], "drafts stay private"
assert len(grid("Day summaries")) == 1 + app.q1("SELECT COUNT(*) FROM summaries WHERE kind='day'")[0]
skin = grid("Tasks")[row_of("Tasks", 1, "Skincare script v2")]
assert (skin[3], skin[4], skin[6]) == ("other", app.ist_day(app.now() + 86400), "In progress")
snapshot, calls = json.dumps(fs.grids), fs.requests
sync()
assert json.dumps(fs.grids) == snapshot and fs.requests - calls == 2, "an idle sync is 2 reads and no writes"

# edits in the sheet reach the app; if both sides changed the same cell the app wins; other cells still merge
put("People", "mailer", 2, "Mailer Two"); put("People", "mailer", 3, "Producer")
sync()
assert (user("mailer")["name"], user("mailer")["role"]) == ("Mailer Two", "Producer")
put("People", "mailer", 2, "Sheet Name"); put("People", "mailer", 3, "Director")
boss("PATCH", f"/api/users/{user('mailer')['id']}", {"name": "App Name"})
sync()
assert (user("mailer")["name"], user("mailer")["role"]) == ("App Name", "Director")
row = grid("People")[row_of("People", 1, "mailer")]
assert row[2] == "App Name" and row[8].startswith("Kept the app's Name")

# a new person typed into the sheet; the password cell is used once and cleared
fs.grids["People"].append(["", "Neha", "Neha K", "Editor", "", "", "neha@example.com", "pass-word-123", ""])
sync()
row = grid("People")[row_of("People", 1, "neha")]
assert row[0] == str(user("neha")["id"]) and row[4:8] == ["No", "Yes", "neha@example.com", ""] and row[8] == "Added"
client("neha", "pass-word-123")
fs.grids["People"].append(["", "short", "Short", "", "", "", "", "tiny", ""])
sync()
assert not user("short") and grid("People")[-1][7] == "" and grid("People")[-1][8].startswith("Not applied: Password")
put("People", "neha", 7, "another-pass-1")
sync()
client("neha", "another-pass-1")
assert grid("People")[row_of("People", 1, "neha")][7:9] == ["", f"Password set {app.datetime.now(app.IST):%d %b %Y}"]

# never half a row: a bad cell stops the whole row, with the reason in Sync note
put("People", "neha", 1, "neha2"); put("People", "neha2", 3, "Lead editor")
sync()
assert user("neha")["role"] == "Editor" and "login ID can't be changed" in grid("People")[row_of("People", 1, "neha2")][8]
put("People", "neha2", 1, "neha")
sync()
assert user("neha")["role"] == "Lead editor" and grid("People")[row_of("People", 1, "neha")][8] == ""
put("People", "boss", 4, "No")
sync()
assert user("boss")["is_admin"] and "at least one active admin" in grid("People")[row_of("People", 1, "boss")][8]
put("People", "boss", 4, "Yes")

# tasks from the sheet: checked like the Tasks page; a date typed in the sheet arrives as a day number
fs.grids["Tasks"].append(["", "Logo sting", "", "nobody", "", "Brand X", "", "", ""])
sync()
assert not app.q1("SELECT 1 FROM tasks WHERE title='Logo sting'") and "'nobody'" in grid("Tasks")[-1][8]
put("Tasks", "Logo sting", 3, "neha")
sync()
t = app.q1("SELECT * FROM tasks WHERE title='Logo sting'")
assert t["assignee_id"] == user("neha")["id"] and t["status"] == "todo" and grid("Tasks")[-1][0] == str(t["id"])
put("Tasks", "Logo sting", 1, "Logo sting v2"); put("Tasks", "Logo sting v2", 4, "next friday")
sync()
assert app.q1("SELECT title FROM tasks WHERE id=?", t["id"])[0] == "Logo sting", "bad date: nothing in the row applies"
put("Tasks", "Logo sting v2", 4, (app.datetime(2026, 11, 20) - app.datetime(1899, 12, 30)).days)
sync()
assert tuple(app.q1("SELECT title, due FROM tasks WHERE id=?", t["id"])) == ("Logo sting v2", "2026-11-20")

# deleting a row: a task is cancelled and stays out of the sheet; undoing the delete brings it back
deleted = fs.grids["Tasks"].pop(row_of("Tasks", 1, "Logo sting v2"))
sync()
assert app.q1("SELECT status FROM tasks WHERE id=?", t["id"])[0] == "cancelled"
sync()
assert all(r[1] != "Logo sting v2" for r in grid("Tasks")), "a deleted row isn't written back"
fs.grids["Tasks"].append(deleted)
sync()
assert app.q1("SELECT status FROM tasks WHERE id=?", t["id"])[0] == "todo"

# a person: deactivated (and logged out); back in the sheet once reactivated in the app
fs.grids["People"].pop(row_of("People", 1, "neha"))
sync()
assert not user("neha")["active"] and not app.q1("SELECT 1 FROM sessions WHERE user_id=?", user("neha")["id"])
boss("PATCH", f"/api/users/{user('neha')['id']}", {"active": True})
sync()
assert grid("People")[row_of("People", 1, "neha")][5] == "Yes"

# many rows gone at once looks like an accident: nothing is deactivated, the rows are put back
app.MAX_SHEET_DELETES = 1
fs.grids["People"][:] = [r for r in fs.grids["People"] if app.cell(r[1]) not in ("neha", "mailer")]
problems = sync()[1]
assert "2 rows disappeared" in problems[0] and user("neha")["active"] and user("mailer")["active"]
assert grid("People")[row_of("People", 1, "neha")][8].startswith("Put back")
app.MAX_SHEET_DELETES = 5

# copies: an edited entry is updated, a deleted one removed, a row deleted by hand comes back, typed rows stay
e = app.q1("SELECT id FROM entries WHERE status='submitted' ORDER BY id")["id"]
app.ex("UPDATE entries SET transcript='Corrected text', edited_at=1 WHERE id=?", e)
fs.grids["Entries"].append(["note to self", "", "", "", "", "", "typed by a founder"])
sync()
assert grid("Entries")[row_of("Entries", 0, str(e))][6:8] == ["Corrected text", "Yes"]
fs.grids["Entries"].pop(row_of("Entries", 0, str(e)))
gone_id = app.q1("SELECT id FROM entries WHERE status='submitted' ORDER BY id DESC")["id"]
app.ex("DELETE FROM entries WHERE id=?", gone_id)
sync()
keys = [r[0] for r in grid("Entries")]
assert str(e) in keys and str(gone_id) not in keys and "note to self" in keys

# a tab whose header row was changed isn't synced (columns could be shifted); the others still are
put("People", "Login ID", 2, "Full name")
assert "People wasn't synced" in sync()[1][0]
put("People", "Login ID", 2, "Name")
assert sync()[1] == []

# the Google sign-in: an RS256 JWT with the claims Google's server-to-server flow asks for
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import padding, rsa  # noqa: E402
k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
head, claims, sig = app.Sheet("x", {"client_email": "sync@demo.iam.gserviceaccount.com", "private_key": pem}).assertion(1_800_000_000).split(".")
unb64 = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
k.public_key().verify(unb64(sig), f"{head}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256())  # raises if wrong
assert json.loads(unb64(head)) == {"alg": "RS256", "typ": "JWT"} and json.loads(unb64(claims)) == {
    "iss": "sync@demo.iam.gserviceaccount.com", "scope": "https://www.googleapis.com/auth/spreadsheets",
    "aud": "https://oauth2.googleapis.com/token", "iat": 1_800_000_000, "exp": 1_800_003_600}

# admin settings: the sheet is set from its link; employees can't touch it
boss("PUT", "/api/sheet", {"sheet": "https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789/edit#gid=0"})
assert boss("GET", "/api/sheet")["sheet_id"] == "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
boss("PUT", "/api/sheet", {"sheet": "not a link"}, expect=400)
other("GET", "/api/sheet", expect=403)
boss("POST", "/api/sheet/sync", expect=409)  # no key file in the test: not set up


# ---------- Slack, against a stand-in for its Web API ----------
class FakeSlack:
    def __init__(self, emails): self.emails, self.posts, self.updates = emails, [], []
    def users_info(self, user): return {"user": {"real_name": f"Slack {user}", "profile": {"email": self.emails.get(user, "")}}}
    def chat_postMessage(self, **kw): self.posts.append(kw); return {"ts": f"{len(self.posts)}.5"}
    def chat_update(self, **kw): self.updates.append(kw); return {"ok": True}


slack = app.slack_web = FakeSlack({"UNEHA": "Neha@Example.com", "UEMP": "emp@example.com", "UNEW": "new@example.com"})
app.slack_bot_user = "UBOT"
real_download, downloads = app.slack_download, []
app.slack_download = lambda url, token: downloads.append(url) or b"x" * 2000
app.slack_tokens = lambda: ("xapp-test", "xoxb-test")
clip = lambda fid, mime="audio/webm": {"id": fid, "mimetype": mime, "size": 2000,
                                      "url_private_download": f"https://files.slack.com/files-pri/T1-{fid}/download/clip.webm"}


def dm(user, files=(), ts=None, **extra):
    app.slack_event({"type": "message", "channel_type": "im", "channel": "D" + user, "user": user,
                     "ts": str(ts or app.now()), "files": list(files), **extra})


press = lambda user, action, value: app.slack_action({"type": "block_actions", "user": {"id": user},
                                                       "actions": [{"action_id": action, "value": value}]})
buttons = lambda upd: [x["action_id"] for b in upd.get("blocks", []) if b["type"] == "actions" for x in b["elements"]]
status = lambda cid: app.q1("SELECT status FROM entries WHERE client_id=?", cid)[0]

# a text message gets the speaking guide, any other file says what works; the bot's own messages and edits are ignored
dm("UNEHA", text="hi")
assert "How to record it" in slack.posts[-1]["text"] and slack.posts[-1]["thread_ts"]
dm("UNEHA", [clip("F0", "image/png")])
assert slack.posts[-1]["text"].startswith("I can only use voice notes")
n = len(slack.posts)
dm("UBOT", bot_id="B1"); dm("UNEHA", subtype="message_changed")
assert len(slack.posts) == n

# a voice note: matched by work email (any case), fetched once, transcribed like an app recording, answered in place
logo = app.q1("SELECT id FROM tasks WHERE title='Logo sting v2'")["id"]
TASK_REPLY[0] = f"T{logo}"
dm("UNEHA", [clip("F1")]); dm("UNEHA", [clip("F1")])  # Slack can send an event twice
eid = app.q1("SELECT id, user_id FROM entries WHERE client_id='slack:F1'")
assert eid["user_id"] == user("neha")["id"] and len(downloads) == 1 and user("neha")["slack_id"] == "UNEHA"
assert slack.posts[-1]["text"].startswith("Got it")
drain()
assert slack.updates[-1]["ts"] == app.q1("SELECT reply_ts FROM slack_notes WHERE entry_id=?", eid["id"])[0]
assert "Rendered three Kling" in slack.updates[-1]["text"] and buttons(slack.updates[-1]) == ["task_done", "task_not_yet", "submit"]
assert any(x["slack"] for x in client("neha", "another-pass-1")("GET", "/api/day")["entries"]), "shown as via Slack"

# buttons: only the person who sent it; the task changes only on their tap; Submit follows the app's rules
press("UNEW", "submit", str(eid["id"]))
assert status("slack:F1") == "draft"
press("UNEHA", "task_done", f"{eid['id']}:{logo}")
assert app.q1("SELECT status FROM tasks WHERE id=?", logo)[0] == "done" and buttons(slack.updates[-1]) == ["submit"]
press("UNEHA", "submit", str(eid["id"]))
assert status("slack:F1") == "submitted" and buttons(slack.updates[-1]) == []
press("UNEHA", "submit", str(eid["id"]))
assert "Couldn't do that: This entry isn't a draft" in json.dumps(slack.updates[-1]["blocks"])

# changed in the app instead: its Slack reply follows, so it never shows old text or a stale Submit button
dm("UNEHA", [clip("F5")]); drain()
f5 = app.q1("SELECT entry_id, reply_ts FROM slack_notes WHERE entry_id=(SELECT id FROM entries WHERE client_id='slack:F5')")
reply = lambda: [u for u in slack.updates if u["ts"] == f5["reply_ts"]][-1]
neha = client("neha", "another-pass-1")
neha("PATCH", f"/api/entries/{f5['entry_id']}", {"transcript": "Fixed in the app."}); drain()
assert "Fixed in the app." in reply()["text"] and "submit" in buttons(reply())
neha("POST", f"/api/entries/{f5['entry_id']}/submit"); drain()
assert buttons(reply()) == [] and "Submitted." in json.dumps(reply()["blocks"])
neha("DELETE", f"/api/entries/{f5['entry_id']}"); drain()
assert reply()["text"] == "Deleted in the app." and reply()["blocks"] == []
dm("UNEHA", [clip("F2")], ts=app.now() - 86400); drain()
assert status("slack:F2") == "submitted", "a note from yesterday goes in as it was said"

# no matching account: kept but not downloaded until an admin links it; a deactivated account gets nothing
app.ex("UPDATE users SET email='emp@example.com' WHERE username='emp'")
dm("UEMP", [clip("F3")])
assert "deactivated" in slack.posts[-1]["text"] and not app.q1("SELECT 1 FROM entries WHERE client_id='slack:F3'")
dm("UNEW", [clip("F4")])
assert "couldn't find" in slack.posts[-1]["text"] and len(downloads) == 3  # F1, F2, F5: not F3 or F4
assert [(w["slack_user"], w["email"], w["notes"]) for w in boss("GET", "/api/slack")["waiting"]] == [("UNEW", "new@example.com", 1)]
other("GET", "/api/slack", expect=403)
boss("POST", "/api/slack/link", {"slack_user": "UNEW", "user": user("neha")["id"]}, expect=409)  # neha is UNEHA
boss("POST", "/api/slack/link", {"slack_user": "UNEW", "user": user("other")["id"]})
drain()
assert tuple(app.q1("SELECT user_id, status FROM entries WHERE client_id='slack:F4'")) == (user("other")["id"], "draft")
assert not boss("GET", "/api/slack")["waiting"] and user("other")["slack_id"] == "UNEW"

# the bot token only ever goes to Slack's own file host, over HTTPS
for bad in ("https://evil.example.com/f", "http://files.slack.com/f", "https://files.slack.com.evil.example/f"):
    try: real_download(bad, "xoxb-test"); raise SystemExit("downloaded from " + bad)
    except ValueError: pass
app.slack_web = None

# database upgrades: a new database starts fully upgraded with no copy; an old one is copied, then upgraded intact
assert app.q1("PRAGMA user_version")[0] == len(app.MIGRATIONS)
assert not (app.DB_PATH.parent / "backups").exists()
old = Path(tempfile.mkdtemp()) / "v2.db"
c = sqlite3.connect(old)
c.executescript(app.SCHEMA)  # SCHEMA without MIGRATIONS is the v2 shape
c.execute("INSERT INTO users(username, name, pw, created) VALUES('kabir', 'Kabir', 'x$y', '2026-11-01')")
c.execute("INSERT INTO entries(user_id, client_id, day, recorded_at, status, transcript) VALUES(1, 'c-1', '2026-11-01', 0, 'submitted', 'hi')")
c.commit(); c.close()
upgrade = lambda: subprocess.run([sys.executable, "-c", "import app; app.init_db()"], cwd=Path(app.__file__).parent,
                                 env={**os.environ, "TS_DB": str(old)}, capture_output=True, text=True)
assert upgrade().returncode == 0
c = sqlite3.connect(old)
assert c.execute("PRAGMA user_version").fetchone()[0] == len(app.MIGRATIONS)
assert c.execute("SELECT username, email FROM users").fetchall() == [("kabir", None)]
assert c.execute("SELECT transcript FROM entries").fetchall() == [("hi",)]
copies = list((old.parent / "backups").glob("before-upgrade-0-to-*.db"))
assert len(copies) == 1 and sqlite3.connect(copies[0]).execute("SELECT COUNT(*) FROM entries").fetchone()[0] == 1
assert upgrade().returncode == 0 and len(list((old.parent / "backups").glob("*.db"))) == 1, "a second start upgrades nothing"
c.execute(f"PRAGMA user_version={len(app.MIGRATIONS) + 1}"); c.commit(); c.close()
assert "newer version" in upgrade().stderr, "old code must refuse a newer database"

print("all checks passed")
