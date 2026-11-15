#!/usr/bin/env python3
"""Timesheet v2: runs on one server; the models (Whisper, Qwen via Ollama) run on that server too.

Speech-to-text: faster-whisper. Summaries: Ollama. Storage: SQLite. HTTPS comes from Caddy in front (deploy/setup.sh).

  .venv/bin/python app.py                                      # serve on 127.0.0.1:8000
  .venv/bin/python app.py adduser admin "Your Name" --admin    # create a login (asks for the password)
  .venv/bin/python app.py selftest                             # check that both models load and run
  .venv/bin/python app.py backup DIR                           # copy the database into DIR (keeps 30)
"""
import argparse, base64, getpass, hashlib, hmac, json, os, queue, re, secrets, sqlite3, sys, threading, time, traceback
import urllib.error, urllib.request
from datetime import datetime, timedelta, timezone
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, quote, urlencode, urlparse

os.environ.setdefault("HF_HUB_OFFLINE", "1")  # Whisper loads from the local cache only (setup downloads it)

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DB_PATH = Path(os.environ.get("TS_DB", ROOT / "data" / "timesheet.db"))
AUDIO_DIR = DB_PATH.parent / "audio"
HOST = os.environ.get("TS_HOST", "127.0.0.1")  # only Caddy, on the same machine, talks to the app
PORT = int(os.environ.get("TS_PORT", "8000"))
PUBLIC_URL = os.environ.get("TS_PUBLIC_URL", "")  # set when behind the HTTPS proxy, e.g. https://timesheet.example.com
WHISPER_MODEL = os.environ.get("TS_WHISPER", "large-v3")  # not large-v3-turbo: turbo can't translate
LLM_MODEL = os.environ.get("TS_LLM", "qwen2.5:14b")  # 7B called tools "clients" and missed blockers in monthly summaries
OLLAMA_URL = os.environ.get("TS_OLLAMA", "http://127.0.0.1:11434")
IST = timezone(timedelta(hours=5, minutes=30))  # ponytail: IST has no DST, so a fixed offset is exact
MAX_AUDIO = 50 * 1024 * 1024
SESSION_SECS = 30 * 86400
# Sent with every response. The app uses no inline scripts or styles, so 'self' covers everything.
# (HSTS is added by Caddy, since it only makes sense over HTTPS.)
SECURITY_HEADERS = [
    ("Content-Security-Policy", "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Permissions-Policy", "microphone=(self), camera=(), geolocation=()"),
]

DEFAULT_GLOSSARY = (
    "Midjourney, Runway, Kling, Veo, Sora, Pika, Luma, Higgsfield, Flux, Stable Diffusion, ComfyUI, LoRA, "
    "ElevenLabs, Suno, HeyGen, CapCut, Premiere Pro, After Effects, DaVinci Resolve, Photoshop, Illustrator, "
    "Canva, Figma, ChatGPT, Claude, Gemini, prompt, render, upscale, inpainting, lip-sync, voiceover, storyboard, "
    "UGC, reel, thumbnail, hook, CTA, A/B variant, client brief, revision, deliverable")
# Shown on top of the Today screen (admins can change it). Its parts line up with the day summary's headings.
# Full sentences on purpose: a spoken label like "Finished: X" gets a pause that Whisper writes as "Finished. X, ...",
# and the summary then lost the task and misplaced the blocker. "I finished X." came out right (tested 15 Nov 2026).
# "Next, ..." also matters: the task check skips sentences that start with it (check_tasks).
DEFAULT_GUIDE = """Hindi, English or a mix is fine. Speak in full sentences, brand by brand:
1. "For <brand or project>, I …" (repeat for each one)
2. "I finished <task name>." (say it the way it's written in My tasks)
3. "I'm blocked on …" (if anything)
4. "Next, …" (what's planned)

Example: "For the shoe brand, I rendered three ad variants in Kling and sent them for review. For the skincare brand, \
I wrote two scripts. I finished the shoe reel CTA. I'm blocked on Figma access. Next, the final edit for the shoe brand.\""""

# Prompts are tuned for a 7B model: short, literal, with the company described as background only
# (otherwise it lists "the company" as a client).
_CONTEXT = "Background: the team makes AI ad films and AI-generated content for client brands. "
_FORMAT = ("Use ONLY facts stated in the input. A client or project must be named in the input; never list the team's "
           "own company as a client. List a tool only if the input names it, and a blocker only if the input says "
           "something is pending, blocked or waiting. Plain text; the only markdown allowed is '- ' bullets.")
PROMPTS = {
    "day": (
        f"{_CONTEXT}You summarise one employee's work day for their manager. The input is what they said "
        "(speech-to-text translated to English; it may contain recognition errors). "
        "Write these headings in this order, skipping any heading with nothing under it:\n"
        "Clients / projects:\nDeliverables:\nTools used:\nBlockers / pending:\n"
        f"Bullets under each heading. Under 120 words. {_FORMAT}"),
    "month": (
        f"{_CONTEXT}You write a monthly work overview of one employee for their manager. The input is their daily "
        "summaries in date order. A later day overrides an earlier one: something pending that was done later is "
        "not a blocker. Write:\n"
        "Overview: 2-3 sentences on what the month was about.\n"
        "Clients / projects: one bullet per client or project with what was delivered.\n"
        "Tools used: one line.\nStill open: bullets, or 'Nothing mentioned'.\n"
        f"Under 250 words. {_FORMAT}"),
    "company": (
        f"{_CONTEXT}You write the company-wide monthly work overview for the founder. "
        "The input is each employee's monthly summary, headed by name and role. Write:\n"
        "Highlights: 3-5 bullets on the biggest work delivered.\n"
        "By client / project: one bullet per client or project with what was delivered.\n"
        "Blockers to act on: group similar items into one bullet (e.g. 'Waiting for client feedback: Brand A, "
        "Brand B'); list every one-off problem separately with who raised it; or 'None mentioned'.\n"
        # No people-per-client or per-person lines: at 40-50 people even 14B mixes up who did what.
        # The Company page lists everyone with a link to their own monthly summary instead.
        f"Under 450 words. {_FORMAT}"),
}
PROMPTS["preview"] = PROMPTS["day"]
# The AI "task done?" check. IDs are written T12 so numbers inside titles ("3 variants") can't pass for an ID.
# Tested on qwen2.5:14b: 30/30 right on 10 notes x 3 runs, once "Next ..." sentences are removed first (see check_tasks).
TASK_CHECK = (
    "You check an employee's work update against their open tasks. The update is speech-to-text translated to English. "
    "Reply with the IDs of the tasks the update clearly says are already finished (done, completed, delivered or sent), "
    "separated by commas, for example: T4, T9. Not finished: anything started, worked on, partly or half done, "
    "in progress, blocked, pending, only mentioned, or planned. If no task is clearly finished, reply: none. "
    "Reply with nothing else.")
# Reports page: the same prompts over any date range ("monthly work overview" -> "work overview", "month" -> "period").
PROMPTS["range"] = PROMPTS["month"].replace("monthly ", "").replace("month", "period")
PROMPTS["range_all"] = PROMPTS["company"].replace("monthly ", "").replace("month", "period")
MAX_RANGE_DAYS = 92  # a person's day summaries for longer than this may not fit the model's context window

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE, name TEXT NOT NULL, role TEXT NOT NULL DEFAULT '',
  pw TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1, created TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(
  token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), expires REAL NOT NULL);
CREATE TABLE IF NOT EXISTS entries(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), client_id TEXT NOT NULL UNIQUE,
  day TEXT NOT NULL, recorded_at REAL NOT NULL,
  status TEXT NOT NULL,              -- queued -> draft -> submitted, or failed
  transcript TEXT NOT NULL DEFAULT '', original TEXT, edited_at REAL, error TEXT);
CREATE INDEX IF NOT EXISTS entries_by_day ON entries(user_id, day);
CREATE INDEX IF NOT EXISTS entries_by_status ON entries(status, day);
CREATE TABLE IF NOT EXISTS summaries(
  kind TEXT NOT NULL,                -- day | preview | month (per user), company (user_id 0)
  user_id INTEGER NOT NULL, period TEXT NOT NULL,
  text TEXT NOT NULL, src TEXT NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(kind, user_id, period));
CREATE TABLE IF NOT EXISTS comments(
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), day TEXT NOT NULL,
  author_id INTEGER NOT NULL REFERENCES users(id), text TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY, title TEXT NOT NULL, details TEXT NOT NULL DEFAULT '',
  assignee_id INTEGER NOT NULL REFERENCES users(id), due TEXT, client TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'todo',  -- todo | in_progress | done | cancelled
  created_by INTEGER NOT NULL REFERENCES users(id), created_at REAL NOT NULL, updated_at REAL NOT NULL, done_at REAL);
CREATE INDEX IF NOT EXISTS tasks_by_assignee ON tasks(assignee_id, status);
CREATE TABLE IF NOT EXISTS task_suggestions(  -- "Looks like you finished X": asked once per note and task
  entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE, task_id INTEGER NOT NULL REFERENCES tasks(id),
  state TEXT NOT NULL DEFAULT 'pending',  -- pending | accepted | dismissed
  PRIMARY KEY(entry_id, task_id));
CREATE TABLE IF NOT EXISTS slack_notes(  -- a note sent on Slack, and the bot's reply that shows its state
  entry_id INTEGER PRIMARY KEY REFERENCES entries(id) ON DELETE CASCADE, channel TEXT NOT NULL, ts TEXT NOT NULL,
  reply_ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS slack_inbox(  -- notes from Slack users with no matching account, until an admin links them
  file_id TEXT PRIMARY KEY, slack_user TEXT NOT NULL, name TEXT NOT NULL, email TEXT NOT NULL, channel TEXT NOT NULL,
  ts TEXT NOT NULL, file TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sheet_rows(  -- Google Sheet sync: each row as last agreed with the sheet (JSON list)
  tab TEXT NOT NULL, key TEXT NOT NULL, data TEXT NOT NULL, gone INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(tab, key));
"""
# Changes to the tables above. Each step runs once, in order, at startup, after a copy of the database is saved.
# Append only: never edit, reorder or remove a step that has shipped, and never edit SCHEMA's tables (add a step).
MIGRATIONS = [
    # 1 (v3): work email, to match Slack users to accounts. NULLs don't clash in a UNIQUE index.
    "ALTER TABLE users ADD COLUMN email TEXT; CREATE UNIQUE INDEX users_email ON users(email);",
    # 2 (v3): the Slack account a person is linked to (found by work email, or linked by an admin).
    "ALTER TABLE users ADD COLUMN slack_id TEXT; CREATE UNIQUE INDEX users_slack ON users(slack_id);",
]

# ---------- storage ----------

_local = threading.local()


def db():
    if not hasattr(_local, "c"):  # one connection per thread; sqlite connections can't be shared across threads
        _local.c = sqlite3.connect(DB_PATH, isolation_level=None, timeout=10)  # autocommit
        _local.c.row_factory = sqlite3.Row
        _local.c.execute("PRAGMA foreign_keys=ON")
    return _local.c


def q(sql, *a): return db().execute(sql, a).fetchall()
def q1(sql, *a): return db().execute(sql, a).fetchone()
def ex(sql, *a): return db().execute(sql, a)


def init_db():
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    db().execute("PRAGMA journal_mode=WAL")  # readers don't block the background workers
    existed = q1("SELECT 1 FROM sqlite_master WHERE name='users'")
    db().executescript(SCHEMA)
    upgrade(existed)


def copy_db(out):
    target = sqlite3.connect(out)
    db().backup(target)  # SQLite's online backup: a consistent copy even mid-write
    target.close()


def upgrade(existed):
    """Run the MIGRATIONS this database hasn't had yet. PRAGMA user_version counts the ones done."""
    v = q1("PRAGMA user_version")[0]
    if v > len(MIGRATIONS): sys.exit(f"{DB_PATH} is from a newer version of this app ({v} upgrades, this code knows "
                                     f"{len(MIGRATIONS)}). Update the code instead of running an old copy.")
    if v == len(MIGRATIONS): return
    if existed:  # a new, empty database needs no copy
        out = DB_PATH.parent / "backups" / f"before-upgrade-{v}-to-{len(MIGRATIONS)}-{datetime.now(IST):%Y-%m-%d-%H%M%S}.db"
        out.parent.mkdir(parents=True, exist_ok=True)
        copy_db(out)  # not named timesheet-*.db, so the nightly keep-30 cleanup leaves it alone
        print(f"Upgrading the database; copy of the old one: {out}")
    for i, sql in enumerate(MIGRATIONS[v:], v + 1):
        try: db().executescript(f"BEGIN; {sql}; PRAGMA user_version={i}; COMMIT;")  # all of a step or none of it
        except Exception:
            if db().in_transaction: db().execute("ROLLBACK")
            raise


def now(): return time.time()
def ist_day(ts=None): return datetime.fromtimestamp(now() if ts is None else ts, IST).strftime("%Y-%m-%d")


class HTTPError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


def valid_day(s):
    if not s: return ist_day()
    try: return datetime.strptime(s, "%Y-%m-%d").strftime("%Y-%m-%d")
    except (TypeError, ValueError): raise HTTPError(400, "Bad date (write it like 2026-11-20)")


def valid_month(s):
    if not s: return ist_day()[:7]
    try: return datetime.strptime(s, "%Y-%m").strftime("%Y-%m")
    except (TypeError, ValueError): raise HTTPError(400, "Bad month")


def valid_range(frm, to):
    """(first day, last day). Neither given: this week, Monday to Sunday (IST)."""
    if not frm and not to:
        t = datetime.strptime(ist_day(), "%Y-%m-%d")
        frm, to = (t - timedelta(days=t.weekday())).strftime("%Y-%m-%d"), (t + timedelta(days=6 - t.weekday())).strftime("%Y-%m-%d")
    frm, to = valid_day(frm), valid_day(to)
    if not 1 <= (datetime.strptime(to, "%Y-%m-%d") - datetime.strptime(frm, "%Y-%m-%d")).days + 1 <= MAX_RANGE_DAYS:
        raise HTTPError(400, f"Pick 1 to {MAX_RANGE_DAYS} days, with From before To")
    return frm, to


def span(kind, period):
    """First and last day a month ('2026-11') or range ('2026-11-03..2026-11-09') summary covers."""
    return tuple(period.split("..")) if kind in ("range", "range_all") else (period + "-01", period + "-31")


# ---------- accounts ----------

def hash_pw(pw, salt=None):
    salt = salt or secrets.token_bytes(16)
    return salt.hex() + "$" + hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1).hex()


def check_pw(pw, stored):
    return hmac.compare_digest(hash_pw(pw, bytes.fromhex(stored.split("$")[0])), stored)


DUMMY_PW = hash_pw("x")  # checked for unknown login IDs, so response time doesn't reveal which IDs exist
_failures = {}  # ponytail: in-memory login throttle (per login ID and per IP), resets on restart


def throttled(key, limit):
    """True once `key` has `limit` failed logins in the last 15 minutes."""
    if len(_failures) > 10_000:  # a bot trying random IDs: drop stale keys so this can't grow forever
        for k in [k for k, ts in _failures.items() if ts[-1] < now() - 900]: del _failures[k]
    recent = [t for t in _failures.get(key, []) if t > now() - 900]
    if recent: _failures[key] = recent
    else: _failures.pop(key, None)
    return len(recent) >= limit


def token_hash(tok): return hashlib.sha256(tok.encode()).hexdigest()


def clean_name(s):
    s = str(s or "").strip()
    if not 0 < len(s) <= 80: raise HTTPError(400, "Name is required (max 80 characters)")
    return s


def clean_pw(s):
    s = str(s or "")
    if len(s) < 10: raise HTTPError(400, "Password must be at least 10 characters")  # the login page is on the internet
    return s


def clean_email(s):
    """Work email (matches Slack users to accounts). Optional: empty means none."""
    s = str(s or "").strip().lower()
    if s and (len(s) > 254 or not re.fullmatch(r"[^@\s]{1,64}@[^@\s]+\.[^@\s]+", s)):
        raise HTTPError(400, "That email doesn't look right")
    return s or None


def user_conflict(err):
    """A UNIQUE clash on users, as a message for the admin."""
    return HTTPError(409, "That email is already used by someone else" if "email" in str(err) else "That login ID is already taken")


def create_user(username, name, password, role="", is_admin=False, email=None):
    username = str(username or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9._-]{2,32}", username):
        raise HTTPError(400, "Login ID: 2-32 characters, letters, numbers, . _ -")
    try:
        return ex("INSERT INTO users(username, name, role, pw, is_admin, created, email) VALUES(?,?,?,?,?,?,?)",
                  username, clean_name(name), str(role or "").strip()[:60], hash_pw(clean_pw(password)),
                  int(bool(is_admin)), ist_day(), clean_email(email)).lastrowid
    except sqlite3.IntegrityError as err:
        raise user_conflict(err)


def user_json(u):
    return {"id": u["id"], "username": u["username"], "name": u["name"], "role": u["role"], "email": u["email"],
            "slack": bool(u["slack_id"]), "is_admin": bool(u["is_admin"]), "active": bool(u["active"]), "created": u["created"]}


def setting(key, default):
    r = q1("SELECT value FROM settings WHERE key=?", key)
    return r["value"] if r else default


def glossary(): return setting("glossary", DEFAULT_GLOSSARY)
def guide(): return setting("guide", DEFAULT_GUIDE)


# ---------- background work ----------

class Jobs:
    """A queue with one worker thread. A key that is already waiting is not queued twice."""

    def __init__(self, fn):
        self.fn, self.q, self.queued, self.running, self.lock = fn, queue.Queue(), set(), None, threading.Lock()

    def put(self, key):
        with self.lock:
            if key in self.queued: return
            self.queued.add(key)
        self.q.put(key)

    def pending(self, key):
        with self.lock: return key in self.queued or key == self.running

    def start(self): threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            key = self.q.get()
            with self.lock: self.queued.discard(key); self.running = key
            try: self.fn(key)
            except Exception: traceback.print_exc()
            finally:
                with self.lock: self.running = None


_whisper = None


def whisper_settings():
    """Where Whisper runs: NVIDIA GPU if there is one, Apple GPU on a Mac, else the CPU. TS_WHISPER_DEVICE overrides."""
    device = os.environ.get("TS_WHISPER_DEVICE")
    if not device:
        if sys.platform == "darwin" and os.uname().machine == "arm64":
            device = "mps"  # ponytail: ~3x faster than CPU on an M4 Pro but "experimental" in CTranslate2
        else:
            import ctranslate2
            device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
    return device, os.environ.get("TS_WHISPER_COMPUTE") or ("float16" if device in ("cuda", "mps") else "int8")


def load_whisper():
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel
        device, compute = whisper_settings()
        _whisper = WhisperModel(WHISPER_MODEL, device=device, compute_type=compute,
                                cpu_threads=os.cpu_count() or 4, local_files_only=True)
    return _whisper


def run_whisper(path):
    """Speech in Hindi, English or Hinglish -> English text."""
    # ponytail: Whisper only reads the last ~220 tokens of the prompt, so a very long glossary loses its start
    # The closing "." matters: Whisper copies the prompt's style, and a bare comma list made it drop every full stop.
    segments, _ = load_whisper().transcribe(str(path), task="translate", initial_prompt="Glossary: " + glossary() + ".",
                                            vad_filter=True)
    return " ".join(s.text.strip() for s in segments).strip()


def transcribe(eid):
    e = q1("SELECT * FROM entries WHERE id=?", eid)
    if not e or e["status"] != "queued": return
    audio = AUDIO_DIR / e["client_id"]
    try:
        text = run_whisper(audio)
    except Exception as err:
        traceback.print_exc()
        ex("UPDATE entries SET status='failed', error=? WHERE id=?", f"Transcription failed: {err}"[:300], eid)
        return slack_reply(eid)  # audio is kept, so the next server start retries it
    audio.unlink(missing_ok=True)  # audio isn't kept once we have the text
    if not text:
        ex("UPDATE entries SET status='failed', error='No speech detected. Discard it and record again.' WHERE id=?", eid)
        return slack_reply(eid)
    # A past day is locked, so it can't be reviewed any more (recorded offline, uploaded late): keep it as said.
    ex("UPDATE entries SET status=?, transcript=? WHERE id=?", "draft" if e["day"] == ist_day() else "submitted", text, eid)
    touch(e["user_id"], e["day"])
    llm_jobs.put(("tasks", eid))


def llm(system, user):
    # Ollama silently drops input that doesn't fit its context window, so size the window to the input
    # (~3 chars per token, plus room for the answer) and refuse rather than summarise half the team.
    # Smallest window that fits: a bigger window costs GPU memory, which Whisper shares.
    need = (len(system) + len(user)) // 3 + 1500
    ctx = next((c for c in (8192, 16384, 32768) if need <= c), None)
    if not ctx: raise ValueError("too much text for one summary (more than ~100 people?)")
    body = json.dumps({"model": LLM_MODEL, "stream": False, "keep_alive": "30m",
                       # num_predict caps a runaway answer (seen once: 15+ minutes looping), which would
                       # otherwise block every summary queued behind it. 450 words is ~600 tokens.
                       "options": {"temperature": 0.2, "num_ctx": ctx, "num_predict": 1500},
                       "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}).encode()
    req = urllib.request.Request(OLLAMA_URL + "/api/chat", body, {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.load(r)["message"]["content"].strip()


def has_drafts(uid, day): return q1("SELECT 1 FROM entries WHERE user_id=? AND day=? AND status='draft'", uid, day) is not None


def summary_text(kind, uid, period):
    r = q1("SELECT text FROM summaries WHERE kind=? AND user_id=? AND period=?", kind, uid, period)
    return r["text"] if r else ""


def sources(kind, uid, period, refresh=False):
    """What a summary is written from. refresh=True (worker only) rebuilds the lower-level summaries first."""
    if kind in ("day", "preview"):
        statuses = "('submitted')" if kind == "day" else "('submitted','draft')"
        return [r[0] for r in q(f"SELECT transcript FROM entries WHERE user_id=? AND day=? AND status IN {statuses} "
                                "ORDER BY recorded_at", uid, period)]
    lo, hi = span(kind, period)
    if kind in ("month", "range"):  # one person: their day summaries
        days = [r[0] for r in q("SELECT DISTINCT day FROM entries WHERE user_id=? AND day BETWEEN ? AND ? "
                                "AND status='submitted' ORDER BY day", uid, lo, hi)]
        if refresh:
            for d in days: build("day", uid, d)
        return [f"{d}:\n{summary_text('day', uid, d)}" for d in days]
    per = "month" if kind == "company" else "range"  # everyone: each person's summary for the same period
    people = q("SELECT id, name, role FROM users WHERE id IN "
               "(SELECT user_id FROM entries WHERE day BETWEEN ? AND ? AND status='submitted') ORDER BY name", lo, hi)
    if refresh:  # ponytail: one long job for the whole team; split per person if it ever blocks day summaries too long
        for p in people: build(per, p["id"], period)
    return [f"{p['name']} ({p['role'] or 'role not set'}):\n{summary_text(per, p['id'], period)}" for p in people]


def digest(parts): return hashlib.sha256("\x00".join(parts).encode()).hexdigest()


def build(kind, uid, period):
    """(Re)write one summary if what it's built from has changed."""
    src = sources(kind, uid, period, refresh=True)
    if kind == "preview" and not has_drafts(uid, period): src = []
    if not src:
        ex("DELETE FROM summaries WHERE kind=? AND user_id=? AND period=?", kind, uid, period)
        return
    h = digest(src)
    row = q1("SELECT src FROM summaries WHERE kind=? AND user_id=? AND period=?", kind, uid, period)
    if row and row["src"] == h: return
    when = {"month": "Month", "company": "Month", "range": "Period", "range_all": "Period"}.get(kind, "Date")
    header = f"{when}: {period.replace('..', ' to ')}\n\n"
    if kind not in ("company", "range_all"):
        u = q1("SELECT name, role FROM users WHERE id=?", uid)
        header = f"Employee: {u['name']} ({u['role'] or 'role not set'})\n" + header
    text = llm(PROMPTS[kind], header + "\n\n".join(src))
    ex("INSERT OR REPLACE INTO summaries VALUES(?,?,?,?,?,?)", kind, uid, period, text, h, now())


OPEN_TASK = "status IN ('todo','in_progress')"


def check_tasks(eid):
    """AI "task done?" check: note which of the person's open tasks this note says are finished. Changes no task;
    the person is asked and decides. No open tasks: no model call."""
    e = q1("SELECT user_id, transcript FROM entries WHERE id=?", eid)
    tasks = q(f"SELECT id, title FROM tasks WHERE assignee_id=? AND {OPEN_TASK}", e["user_id"]) if e else []
    # Plans aren't work done. The 14B model still matched "Next, the final edit" to the task "Final edit" even when told
    # not to, so the plan sentences the speaking guide asks for are dropped before it reads the note.
    # Speech can come back with no full stops, so a capitalised "Next"/"Tomorrow" also starts a new sentence.
    # ponytail: only sentences starting "Next"/"Tomorrow"; a plan said another way can still be suggested (one tap: Not yet)
    parts = re.split(r"(?<=[.!?])\s+|\s+(?=(?:Next|Tomorrow)\b)", e["transcript"] if e else "")
    said = " ".join(s for s in parts if not re.match(r"(?i)(next|tomorrow)\b", s))
    if not tasks or not said: return
    reply = llm(TASK_CHECK, f"Update:\n{said}\n\nOpen tasks:\n" + "\n".join(f"T{t['id']}: {t['title']}" for t in tasks))
    if not re.fullmatch(r"\s*(none|T\d+(\s*,\s*T\d+)*)\s*\.?\s*", reply, re.I): return  # anything chattier: ignored
    for tid in {int(x) for x in re.findall(r"\d+", reply)} & {t["id"] for t in tasks}:  # only this person's open tasks
        ex("INSERT OR IGNORE INTO task_suggestions(entry_id, task_id) VALUES(?,?)", eid, tid)


llm_errors = {}


def run_build(key):
    try:
        check_tasks(key[1]) if key[0] == "tasks" else build(*key)
        llm_errors.pop(key, None)
    except Exception as e:
        llm_errors[key] = f"Summary failed ({e}). Is Ollama running with {LLM_MODEL}?"[:300]
        raise
    finally:  # the task check is the last step for a note: if it came from Slack, its reply now shows the result
        if key[0] == "tasks": slack_reply(key[1])


whisper_jobs, llm_jobs = Jobs(transcribe), Jobs(run_build)


def touch(uid, day):
    """Entries for (uid, day) changed: refresh the admin's day summary and the employee's draft preview."""
    llm_jobs.put(("day", uid, day))
    llm_jobs.put(("preview", uid, day))


def summary_view(kind, uid, period, auto=True):
    key = (kind, uid, period)
    src = sources(kind, uid, period)
    row = q1("SELECT text, src, updated_at FROM summaries WHERE kind=? AND user_id=? AND period=?", *key)
    stale = bool(src) and (row is None or row["src"] != digest(src))
    if stale and auto: llm_jobs.put(key)  # self-healing: whatever reads a stale summary gets it rebuilt
    shown = row if row and src else None
    return {"text": shown["text"] if shown else "", "updated_at": shown["updated_at"] if shown else None,
            "stale": stale, "pending": llm_jobs.pending(key), "error": llm_errors.get(key)}


def sweep():
    """Drafts from earlier days are locked now: keep them as submitted rather than lose them."""
    today = ist_day()
    left = q("SELECT DISTINCT user_id, day FROM entries WHERE status='draft' AND day<?", today)
    if left:
        ex("UPDATE entries SET status='submitted' WHERE status='draft' AND day<?", today)
        for r in left: touch(r["user_id"], r["day"])


# ---------- HTTP ----------

ROUTES = []


def route(method, path, auth="user"):
    def deco(fn):
        ROUTES.append((method, re.compile(path + "$"), auth, fn))
        return fn
    return deco


MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".json": "application/manifest+json", ".png": "image/png"}


def cookie(value, max_age):
    secure = "; Secure" if PUBLIC_URL.startswith("https://") else ""  # browsers then never send it over plain HTTP
    return ("Set-Cookie", f"sid={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}{secure}")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self): self.handle_any("GET")
    def do_HEAD(self): self.handle_any("GET")  # same as GET; reply() and static() skip the body
    def do_POST(self): self.handle_any("POST")
    def do_PUT(self): self.handle_any("PUT")
    def do_PATCH(self): self.handle_any("PATCH")
    def do_DELETE(self): self.handle_any("DELETE")

    def log_request(self, code="-", size="-"):
        if not isinstance(code, int) or code >= 400: super().log_request(code, size)

    def end_headers(self):
        for k, v in SECURITY_HEADERS: self.send_header(k, v)
        super().end_headers()

    def client_ip(self):
        # Behind Caddy every request comes from 127.0.0.1; Caddy puts the real address last in X-Forwarded-For.
        if PUBLIC_URL and self.client_address[0] == "127.0.0.1" and self.headers.get("X-Forwarded-For"):
            return self.headers["X-Forwarded-For"].split(",")[-1].strip()
        return self.client_address[0]

    def handle_any(self, method):
        url, self.extra, self.token = urlparse(self.path), [], None
        if not url.path.startswith("/api/"):
            return self.static(url.path) if method == "GET" else self.reply(405, {"error": "Method not allowed"})
        try:
            for m, rx, auth, fn in ROUTES:
                if m == method and (match := rx.match(url.path)): break
            else:
                raise HTTPError(404, "Not found")
            self.args = {k: v[0] for k, v in parse_qs(url.query).items()}
            self.user = self.session_user()
            if auth and not self.user: raise HTTPError(401, "Please log in")
            if auth == "admin" and not self.user["is_admin"]: raise HTTPError(403, "Admins only")
            sweep()
            self.reply(200, fn(self, *match.groups()) or {"ok": True})
        except HTTPError as e:
            self.reply(e.code, {"error": e.msg})
        except ValueError:
            self.reply(400, {"error": "Bad request"})
        except Exception:
            traceback.print_exc()
            self.reply(500, {"error": "Something went wrong on the server"})

    def reply(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in self.extra: self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD": self.wfile.write(data)

    def static(self, path):
        if path == "/favicon.ico": path = "/icon-192.png"  # browsers ask for this on every page load
        f = (STATIC / (path.lstrip("/") or "index.html")).resolve()
        if not f.is_file() or STATIC not in f.parents:
            return self.reply(404, {"error": "Not found"})
        data = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(f.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if self.command != "HEAD": self.wfile.write(data)

    def body(self, limit):
        n = int(self.headers.get("Content-Length") or 0)
        if n > limit: raise HTTPError(413, "Upload is too large")
        return self.rfile.read(n)

    def body_json(self):
        try: b = json.loads(self.body(1_000_000) or b"{}")
        except ValueError: raise HTTPError(400, "Bad JSON")
        if not isinstance(b, dict): raise HTTPError(400, "Bad JSON")
        return b

    def session_user(self):
        try: tok = SimpleCookie(self.headers.get("Cookie", "")).get("sid")
        except CookieError: return None
        if not tok: return None
        self.token = tok.value
        return q1("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id "
                  "WHERE s.token=? AND s.expires>? AND u.active=1", token_hash(tok.value), now())


def target_user(h, uid):
    uid = int(uid or h.user["id"])
    if uid != h.user["id"] and not h.user["is_admin"]: raise HTTPError(403, "Not allowed")
    u = q1("SELECT * FROM users WHERE id=?", uid)
    if not u: raise HTTPError(404, "No such person")
    return u


def own_entry(uid, eid, today_only=True):
    """Person uid's entry eid, if they may change it now. The web app and Slack buttons both go through this."""
    e = q1("SELECT * FROM entries WHERE id=? AND user_id=?", int(eid), uid)
    if not e: raise HTTPError(404, "Entry not found")
    if e["status"] == "queued": raise HTTPError(409, "Still transcribing. Try again in a moment.")
    if today_only and e["day"] != ist_day(): raise HTTPError(403, "Only today's entries can be changed")
    return e


def submit(uid, eid):
    e = own_entry(uid, eid)
    if e["status"] != "draft": raise HTTPError(409, "This entry isn't a draft")
    ex("UPDATE entries SET status='submitted' WHERE id=?", e["id"])
    touch(e["user_id"], e["day"])


def entry_json(e, owner):
    d = {"id": e["id"], "status": e["status"], "transcript": e["transcript"], "recorded_at": e["recorded_at"],
         "edited": e["edited_at"] is not None, "slack": e["client_id"].startswith("slack:")}
    if owner: d["error"] = e["error"]
    else: d["original"] = e["original"]  # what the admin may have read before the employee edited it
    return d


@route("POST", "/api/login", auth=None)
def login(h):
    b = h.body_json()
    username, pw = str(b.get("username", "")).strip().lower(), str(b.get("password", ""))
    # 5 tries per login ID; 50 per address, high enough for a whole office sharing one internet connection.
    keys = [("id:" + username, 5), ("ip:" + h.client_ip(), 50)]
    if any(throttled(k, limit) for k, limit in keys): raise HTTPError(429, "Too many attempts. Try again in 15 minutes.")
    u = q1("SELECT * FROM users WHERE username=? AND active=1", username)
    if not check_pw(pw, u["pw"] if u else DUMMY_PW) or not u:
        for k, _ in keys: _failures.setdefault(k, []).append(now())
        raise HTTPError(401, "Wrong login ID or password")
    tok = secrets.token_urlsafe(32)
    ex("DELETE FROM sessions WHERE expires<?", now())
    ex("INSERT INTO sessions VALUES(?,?,?)", token_hash(tok), u["id"], now() + SESSION_SECS)
    h.extra.append(cookie(tok, SESSION_SECS))
    return {"me": user_json(u), "today": ist_day()}


@route("POST", "/api/logout", auth=None)
def logout(h):
    if h.token: ex("DELETE FROM sessions WHERE token=?", token_hash(h.token))
    h.extra.append(cookie("", 0))


@route("GET", "/api/me")
def me(h): return {"me": user_json(h.user), "today": ist_day()}


@route("GET", "/api/health", auth=None)
def health(h):
    """For an uptime monitor. Says nothing about users or data."""
    try:
        with urllib.request.urlopen(OLLAMA_URL + "/api/tags", timeout=2): ollama = True
    except OSError:
        ollama = False
    return {"ok": True, "ollama": ollama,
            "waiting": {"transcriptions": whisper_jobs.q.qsize(), "summaries": llm_jobs.q.qsize()}}


@route("POST", "/api/entries")
def upload(h):
    cid = h.headers.get("X-Client-Id", "")
    if not re.fullmatch(r"[A-Za-z0-9-]{8,64}", cid): raise HTTPError(400, "Missing recording id")
    if e := q1("SELECT id, user_id FROM entries WHERE client_id=?", cid):
        if e["user_id"] != h.user["id"]: raise HTTPError(409, "Recording id clash")
        return {"id": e["id"]}  # the phone retried an upload we already have
    data = h.body(MAX_AUDIO)
    if len(data) < 1000: raise HTTPError(400, "That recording is empty")
    try: recorded = float(h.headers.get("X-Recorded-At", "")) / 1000
    except ValueError: recorded = 0
    # The recording's own time decides its day (offline uploads arrive late). Trust the device clock within reason.
    recorded = min(recorded, now()) if now() - 90 * 86400 < recorded < now() + 300 else now()
    (AUDIO_DIR / cid).write_bytes(data)
    try:
        eid = ex("INSERT INTO entries(user_id, client_id, day, recorded_at, status) VALUES(?,?,?,?,'queued')",
                 h.user["id"], cid, ist_day(recorded), recorded).lastrowid
    except sqlite3.IntegrityError:  # two copies of the same upload raced
        return {"id": q1("SELECT id FROM entries WHERE client_id=?", cid)["id"]}
    whisper_jobs.put(eid)
    return {"id": eid}


@route("PATCH", r"/api/entries/(\d+)")
def edit_entry(h, eid):
    e = own_entry(h.user["id"], eid)
    if e["status"] == "failed": raise HTTPError(409, "Discard this one and record again")
    text = str(h.body_json().get("transcript", "")).strip()
    if not 0 < len(text) <= 20000: raise HTTPError(400, "The text can't be empty")
    if text == e["transcript"]: return
    if e["status"] == "submitted":  # the admin may have read it already: keep the first version and flag the edit
        ex("UPDATE entries SET transcript=?, original=COALESCE(original, transcript), edited_at=? WHERE id=?",
           text, now(), e["id"])
    else:
        ex("UPDATE entries SET transcript=? WHERE id=?", text, e["id"])
    touch(e["user_id"], e["day"])


@route("POST", r"/api/entries/(\d+)/submit")
def submit_entry(h, eid): submit(h.user["id"], eid)


@route("DELETE", r"/api/entries/(\d+)")
def delete_entry(h, eid):
    e = own_entry(h.user["id"], eid, today_only=False)
    if e["status"] != "failed" and e["day"] != ist_day(): raise HTTPError(403, "Only today's entries can be changed")
    ex("DELETE FROM entries WHERE id=?", e["id"])
    (AUDIO_DIR / e["client_id"]).unlink(missing_ok=True)
    touch(e["user_id"], e["day"])


@route("GET", "/api/day")
def day_view(h):
    u, day = target_user(h, h.args.get("user")), valid_day(h.args.get("date"))
    owner = u["id"] == h.user["id"]
    rows = q("SELECT * FROM entries WHERE user_id=? AND day=? " + ("" if owner else "AND status='submitted' ")
             + "ORDER BY recorded_at", u["id"], day)
    drafts = owner and any(r["status"] == "draft" for r in rows)
    res = {"date": day, "today": ist_day(), "user": user_json(u), "editable": owner and day == ist_day(),
           "entries": [entry_json(r, owner) for r in rows], "preview": drafts,
           "summary": summary_view("preview" if drafts else "day", u["id"], day)}
    if res["editable"]:  # the Today screen: how to speak the update, my tasks, and "did you finish X?"
        res["guide"] = guide()
        res["tasks"] = [dict(r) for r in q(
            f"SELECT * FROM tasks WHERE assignee_id=? AND ({OPEN_TASK} OR status='done' AND done_at>?) "
            "ORDER BY status='done', due IS NULL, due, id", u["id"], now() - 7 * 86400)]
        res["suggestions"] = [dict(r) for r in q(
            "SELECT s.entry_id, s.task_id, t.title FROM task_suggestions s JOIN tasks t ON t.id=s.task_id "
            f"JOIN entries e ON e.id=s.entry_id WHERE s.state='pending' AND e.user_id=? AND t.assignee_id=? AND t.{OPEN_TASK}",
            u["id"], u["id"])]
    if not owner:  # comments are for admins only; employees never see them
        res["comments"] = [dict(r) for r in q(
            "SELECT c.text, c.created_at, a.name AS author FROM comments c JOIN users a ON a.id=c.author_id "
            "WHERE c.user_id=? AND c.day=? ORDER BY c.created_at", u["id"], day)]
    return res


@route("GET", "/api/month")
def month_view(h):
    u, m = target_user(h, h.args.get("user")), valid_month(h.args.get("m"))
    statuses = "('draft','submitted')" if u["id"] == h.user["id"] else "('submitted')"
    days = {r["day"]: r["n"] for r in q(f"SELECT day, COUNT(*) AS n FROM entries WHERE user_id=? AND day LIKE ? "
                                        f"AND status IN {statuses} GROUP BY day", u["id"], m + "-%")}
    res = {"month": m, "today": ist_day(), "user": user_json(u), "days": days}
    if h.user["is_admin"]: res["summary"] = summary_view("month", u["id"], m, auto=False)
    return res


@route("GET", "/api/company", auth="admin")
def company_view(h):
    m = valid_month(h.args.get("m"))
    people = q("SELECT u.id, u.name, u.role, COUNT(DISTINCT e.day) AS days, COUNT(e.id) AS entries FROM users u "
               "LEFT JOIN entries e ON e.user_id=u.id AND e.status='submitted' AND e.day LIKE ? "
               "WHERE u.active=1 GROUP BY u.id ORDER BY u.name", m + "-%")
    return {"month": m, "today": ist_day(), "people": [dict(p) for p in people],
            "summary": summary_view("company", 0, m, auto=False)}


@route("GET", "/api/range", auth="admin")
def range_view(h):
    """Reports page: who logged which days in a date range; for one person also each day's summary and exact words."""
    lo, hi = valid_range(h.args.get("from"), h.args.get("to"))
    period, uid = f"{lo}..{hi}", int(h.args.get("user") or 0)
    counts = {}
    for r in q("SELECT user_id, day, COUNT(*) AS n FROM entries WHERE status='submitted' AND day BETWEEN ? AND ? "
               "GROUP BY user_id, day", lo, hi):
        counts.setdefault(r["user_id"], {})[r["day"]] = r["n"]
    people = [{"id": u["id"], "name": u["name"], "role": u["role"], "created": u["created"], "days": counts.get(u["id"], {})}
              for u in q("SELECT * FROM users ORDER BY name COLLATE NOCASE") if u["active"] or u["id"] in counts]
    res = {"from": lo, "to": hi, "today": ist_day(), "people": people}
    if not uid: return res | {"summary": summary_view("range_all", 0, period, auto=False)}
    u, days = target_user(h, uid), {}
    for e in q("SELECT * FROM entries WHERE user_id=? AND status='submitted' AND day BETWEEN ? AND ? ORDER BY recorded_at",
               uid, lo, hi):
        if e["day"] not in days: days[e["day"]] = {"entries": [], "summary": summary_view("day", uid, e["day"])}
        days[e["day"]]["entries"].append(entry_json(e, False))
    return res | {"user": user_json(u), "days": days, "summary": summary_view("range", uid, period, auto=False)}


@route("POST", "/api/summaries", auth="admin")
def generate_summary(h):
    """Month (or company) summary from {month, user?}; date-range summary from {from, to, user?}. No user: everyone."""
    b = h.body_json()
    uid = int(b.get("user") or 0)
    if uid: target_user(h, uid)
    if "from" in b:
        period = "..".join(valid_range(b.get("from"), b.get("to")))
        return llm_jobs.put(("range", uid, period) if uid else ("range_all", 0, period))
    m = valid_month(b.get("month"))
    llm_jobs.put(("month", uid, m) if uid else ("company", 0, m))


@route("GET", "/api/users", auth="admin")
def list_users(h):
    rows = q("SELECT u.*, (SELECT COUNT(*) FROM entries e WHERE e.user_id=u.id AND e.day=? AND e.status='submitted') "
             "AS today_count FROM users u ORDER BY u.name COLLATE NOCASE", ist_day())
    return {"users": [user_json(r) | {"today_count": r["today_count"]} for r in rows],
            "roles": sorted({r["role"] for r in rows if r["role"]})}


@route("POST", "/api/users", auth="admin")
def add_user(h):
    b = h.body_json()
    return {"id": create_user(b.get("username"), b.get("name"), b.get("password"), b.get("role"), b.get("is_admin"),
                              b.get("email"))}


@route("PATCH", r"/api/users/(\d+)", auth="admin")
def edit_user(h, uid):
    u, b = target_user(h, uid), h.body_json()
    if u["id"] == h.user["id"] and (b.get("active") is False or b.get("is_admin") is False):
        raise HTTPError(400, "You can't deactivate yourself or remove your own admin access")
    update_user(u, b, token_hash(h.token or ""))


def update_user(u, b, keep_token=""):
    """Change account u from b (any of name, role, password, is_admin, active, email). The Team page and the sheet."""
    name = clean_name(b["name"]) if "name" in b else u["name"]
    pw = hash_pw(clean_pw(b["password"])) if b.get("password") else u["pw"]
    role = str(b["role"] or "").strip()[:60] if "role" in b else u["role"]
    is_admin = int(bool(b["is_admin"])) if "is_admin" in b else u["is_admin"]
    active = int(bool(b["active"])) if "active" in b else u["active"]
    email = clean_email(b["email"]) if "email" in b else u["email"]
    if u["is_admin"] and u["active"] and not (is_admin and active) and \
            not q1("SELECT 1 FROM users WHERE is_admin=1 AND active=1 AND id<>?", u["id"]):
        raise HTTPError(400, "There must always be at least one active admin")
    try: ex("UPDATE users SET name=?, role=?, pw=?, is_admin=?, active=?, email=? WHERE id=?",
            name, role, pw, is_admin, active, email, u["id"])
    except sqlite3.IntegrityError as err: raise user_conflict(err)
    if b.get("password") or not active:  # new password or deactivated: log them out everywhere (except this browser)
        ex("DELETE FROM sessions WHERE user_id=? AND token<>?", u["id"], keep_token)


@route("POST", "/api/comments", auth="admin")
def add_comment(h):
    b = h.body_json()
    u, day, text = target_user(h, b.get("user")), valid_day(b.get("date")), str(b.get("text", "")).strip()
    if not 0 < len(text) <= 2000: raise HTTPError(400, "A comment must be 1-2000 characters")
    ex("INSERT INTO comments(user_id, day, author_id, text, created_at) VALUES(?,?,?,?,?)",
       u["id"], day, h.user["id"], text, now())


TASK_STATUSES = ("todo", "in_progress", "done", "cancelled")


def task_fields(b, admin):
    """The task fields in request body b, checked. Employees may only change the status (and not cancel)."""
    if not admin and (set(b) - {"status"} or b.get("status") == "cancelled"):
        raise HTTPError(403, "Only an admin can change that")
    f = {}
    if "title" in b:
        f["title"] = " ".join(str(b["title"] or "").split())
        if not 0 < len(f["title"]) <= 200: raise HTTPError(400, "A task needs a title (max 200 characters)")
    if "details" in b: f["details"] = str(b["details"] or "").strip()[:4000]
    if "client" in b: f["client"] = " ".join(str(b["client"] or "").split())[:80]
    if "due" in b: f["due"] = valid_day(b["due"]) if b["due"] else None
    if "assignee" in b:
        u = q1("SELECT id FROM users WHERE id=? AND active=1", int(b["assignee"] or 0))
        if not u: raise HTTPError(400, "Pick an active person to give it to")
        f["assignee_id"] = u["id"]
    if "status" in b:
        if b["status"] not in TASK_STATUSES: raise HTTPError(400, "Bad status")
        f["status"] = b["status"]
    return f


@route("GET", "/api/tasks")
def list_tasks(h):
    """Employees: their own tasks only. Admins: filter by ?user=, ?status= (or 'open'), ?overdue=1."""
    where, args = [], []
    uid = int(h.args.get("user") or 0) if h.user["is_admin"] else h.user["id"]
    if uid: where.append("t.assignee_id=?"); args.append(uid)
    st = h.args.get("status", "")
    if st == "open": where.append(f"t.{OPEN_TASK}")
    elif st: where.append("t.status=?"); args.append(st)
    if h.args.get("overdue"): where.append(f"t.{OPEN_TASK} AND t.due<?"); args.append(ist_day())
    rows = q("SELECT t.*, u.name AS assignee, u.active AS assignee_active FROM tasks t JOIN users u ON u.id=t.assignee_id"
             + "".join(f" {'WHERE' if i == 0 else 'AND'} {w}" for i, w in enumerate(where))
             + " ORDER BY t.status IN ('done','cancelled'), t.due IS NULL, t.due, t.id", *args)
    return {"tasks": [dict(r) | {"assignee_active": bool(r["assignee_active"])} for r in rows], "today": ist_day()}


@route("POST", "/api/tasks", auth="admin")
def add_task(h): return {"id": create_task(task_fields(h.body_json(), True), h.user["id"])}


def create_task(f, by):
    """f: checked fields from task_fields (column names come from there, never from a request)."""
    if "title" not in f or "assignee_id" not in f: raise HTTPError(400, "A task needs a title and a person")
    f.setdefault("status", "todo")
    t = now()
    f |= {"created_by": by, "created_at": t, "updated_at": t, "done_at": t if f["status"] == "done" else None}
    return ex(f"INSERT INTO tasks({', '.join(f)}) VALUES({', '.join('?' * len(f))})", *f.values()).lastrowid


@route("PATCH", r"/api/tasks/(\d+)")
def edit_task(h, tid):
    t = q1("SELECT * FROM tasks WHERE id=?", int(tid))
    if not t or not (h.user["is_admin"] or t["assignee_id"] == h.user["id"]): raise HTTPError(404, "Task not found")
    change_task(t, task_fields(h.body_json(), h.user["is_admin"]))


def change_task(t, f):
    if not f: return
    if f.get("status", t["status"]) != t["status"]: f["done_at"] = now() if f["status"] == "done" else None
    f["updated_at"] = now()
    ex(f"UPDATE tasks SET {', '.join(k + '=?' for k in f)} WHERE id=?", *f.values(), t["id"])


@route("POST", r"/api/suggestions/(\d+)/(\d+)/(accept|dismiss)")
def answer_suggestion(h, eid, tid, answer): answer_task(h.user["id"], eid, tid, answer)


def answer_task(uid, eid, tid, answer):
    """Person uid's answer to "Looks like you finished X. Mark it done?" (app or Slack). Only this changes the task."""
    s = q1("SELECT t.status FROM task_suggestions s JOIN entries e ON e.id=s.entry_id JOIN tasks t ON t.id=s.task_id "
           "WHERE s.entry_id=? AND s.task_id=? AND e.user_id=? AND t.assignee_id=?", int(eid), int(tid), uid, uid)
    if not s: raise HTTPError(404, "Not found")
    ex("UPDATE task_suggestions SET state=? WHERE entry_id=? AND task_id=?",
       {"accept": "accepted", "dismiss": "dismissed"}[answer], int(eid), int(tid))
    if answer == "accept" and s["status"] in ("todo", "in_progress"):
        ex("UPDATE tasks SET status='done', done_at=?, updated_at=? WHERE id=?", now(), now(), int(tid))


@route("GET", "/api/glossary", auth="admin")
def get_glossary(h): return {"text": glossary(), "guide": guide()}


@route("PUT", "/api/guide", auth="admin")
def put_guide(h):
    text = str(h.body_json().get("text", "")).strip()
    if len(text) > 2000: raise HTTPError(400, "Keep the guide under 2000 characters")
    if text: ex("INSERT OR REPLACE INTO settings VALUES('guide', ?)", text)
    else: ex("DELETE FROM settings WHERE key='guide'")  # empty: back to the default


@route("PUT", "/api/glossary", auth="admin")
def put_glossary(h):
    terms = [t.strip() for t in re.split(r"[,\n]", str(h.body_json().get("text", ""))) if t.strip()]
    ex("INSERT OR REPLACE INTO settings VALUES('glossary', ?)", ", ".join(terms)[:3000])


# ---------- Google Sheet sync ----------
# The sheet is the admins' control panel and live mirror; SQLite stays the engine underneath. Off unless an admin
# sets a sheet and the service-account key file exists. People and Tasks sync both ways, cell by cell, against the
# value both sides last agreed on (sheet_rows); Entries and Day summaries are copies, updated row by row.

GOOGLE_KEY = Path(os.environ.get("TS_GOOGLE_KEY", DB_PATH.parent / "google-key.json"))
SYNC_EVERY = 60  # seconds. A sync is ~3-6 API calls; Google allows 60 reads and 60 writes a minute per account.
MAX_SHEET_DELETES = 5  # more rows than this gone from a tab at once looks like an accident: they're put back
PEOPLE_COLS = ["ID", "Login ID", "Name", "Role", "Admin", "Active", "Work email", "New password", "Sync note"]
TASK_COLS = ["ID", "Title", "Details", "Assignee", "Due", "Client / project", "Status", "Updated", "Sync note"]
ENTRY_COLS = ["Entry ID", "Date", "Login ID", "Name", "Recorded at (IST)", "Source", "Transcript", "Edited"]
DAYSUM_COLS = ["Date", "Login ID", "Name", "Summary"]
STATUS_LABEL = {"todo": "To do", "in_progress": "In progress", "done": "Done", "cancelled": "Cancelled"}
sync_status = {"at": None, "error": None, "result": ""}


def a1(tab, cells): return f"'{tab}'!{cells}"
def b64url(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


class Sheet:
    """The few Google Sheets API v4 calls the sync needs, over plain HTTPS, signed in as a service account."""
    API = "https://sheets.googleapis.com/v4/spreadsheets/"

    def __init__(self, sheet_id, key):  # key: the service account's JSON key file, parsed
        self.id, self.key, self.token, self.expires = sheet_id, key, None, 0

    def assertion(self, t=None):
        """The signed JWT that Google swaps for an access token (OAuth 2.0 for server-to-server applications)."""
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        t = int(t or time.time())
        claims = {"iss": self.key["client_email"], "scope": "https://www.googleapis.com/auth/spreadsheets",
                  "aud": "https://oauth2.googleapis.com/token", "iat": t, "exp": t + 3600}
        msg = b64url(b'{"alg":"RS256","typ":"JWT"}') + "." + b64url(json.dumps(claims).encode())
        pem = serialization.load_pem_private_key(self.key["private_key"].encode(), None)
        return msg + "." + b64url(pem.sign(msg.encode(), padding.PKCS1v15(), hashes.SHA256()))

    def call(self, method, path, body=None):
        if time.time() > self.expires - 300:  # an access token lasts an hour: get a new one 5 minutes early
            form = urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": self.assertion()})
            tok = self.http(urllib.request.Request("https://oauth2.googleapis.com/token", form.encode()))
            self.token, self.expires = tok["access_token"], time.time() + tok.get("expires_in", 3600)
        return self.http(urllib.request.Request(
            self.API + self.id + path, None if body is None else json.dumps(body).encode(),
            {"Authorization": "Bearer " + self.token, "Content-Type": "application/json"}, method=method))

    @staticmethod
    def http(req):
        try:
            with urllib.request.urlopen(req, timeout=150) as r: return json.load(r)  # Google gives up after 180 s
        except urllib.error.HTTPError as e:  # ponytail: no retry; the next sync a minute later is the retry
            try: body = json.load(e)
            except ValueError: body = {}
            err = body.get("error")  # Sheets: {"error": {"message"}}; sign-in: {"error", "error_description"}
            msg = err.get("message") if isinstance(err, dict) else " ".join(filter(None, [err, body.get("error_description")]))
            raise RuntimeError(f"Google said {e.code}: {msg or e.reason}")

    def tabs(self):
        return {s["properties"]["title"]: s["properties"]["sheetId"]
                for s in self.call("GET", "?fields=sheets.properties(sheetId,title)")["sheets"]}

    def add_tabs(self, titles):
        self.call("POST", ":batchUpdate", {"requests": [{"addSheet": {"properties": {"title": t}}} for t in titles]})

    def read(self, ranges):  # dates come back as day numbers, checkboxes as true/false: see cell() and sheet_date()
        qs = urlencode([("ranges", r) for r in ranges] + [("valueRenderOption", "UNFORMATTED_VALUE"),
                                                          ("dateTimeRenderOption", "SERIAL_NUMBER")])
        return [v.get("values", []) for v in self.call("GET", "/values:batchGet?" + qs)["valueRanges"]]

    def write(self, data):  # RAW: stored exactly as sent, never parsed as dates or formulas
        self.call("POST", "/values:batchUpdate", {"valueInputOption": "RAW", "data": [{"range": r, "values": v} for r, v in data]})

    def append(self, tab, rows):
        self.call("POST", f"/values/{quote(a1(tab, 'A1'))}:append?valueInputOption=RAW&insertDataOption=INSERT_ROWS",
                  {"values": rows})

    def delete_rows(self, rows):  # [(sheetId, row number as shown in the sheet)]; bottom first, so numbers don't shift
        self.call("POST", ":batchUpdate", {"requests": [
            {"deleteDimension": {"range": {"sheetId": sid, "dimension": "ROWS", "startIndex": n - 1, "endIndex": n}}}
            for sid, n in sorted(rows, key=lambda r: -r[1])]})


def cell(v):
    """A value read from the sheet, as text. Checkboxes are Yes/No; 12.0 is 12."""
    if v is None: return ""
    if isinstance(v, bool): return "Yes" if v else "No"
    if isinstance(v, float) and v.is_integer(): v = int(v)
    return str(v).strip()


def sheet_date(v):
    """A date typed into the sheet comes back as a day number counted from 30 Dec 1899; text is kept as is."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return (datetime(1899, 12, 30) + timedelta(days=int(v))).strftime("%Y-%m-%d")
    return cell(v)


def yes_no(s, default):  # anything else is kept, so the check reports it
    return {"": default, "yes": "Yes", "y": "Yes", "true": "Yes", "no": "No", "n": "No", "false": "No"}.get(s.lower(), s)


def people_load(uid=None):
    """Each person as the People tab shows them: Login ID, Name, Role, Admin, Active, Work email."""
    rows = q("SELECT * FROM users WHERE id=?", uid) if uid else q("SELECT * FROM users")
    return {r["id"]: [r["username"], r["name"], r["role"], "Yes" if r["is_admin"] else "No", "Yes" if r["active"] else "No",
                      r["email"] or ""] for r in rows}


def people_norm(v):
    s = [cell(x) for x in v]
    return [s[0].lower(), s[1], s[2], yes_no(s[3], "No"), yes_no(s[4], "Yes"), s[5].lower()]


def people_apply(uid, s, changed, password):
    """A person's row from the sheet: all of it is checked before anything changes."""
    for f in (3, 4):
        if s[f] not in ("Yes", "No"): raise HTTPError(400, f"{PEOPLE_COLS[f + 1]} must be Yes or No")
    if uid is None:
        if not password: raise HTTPError(400, "Type a New password (at least 10 characters) to add this person")
        uid = create_user(s[0], s[1], password, s[2], s[3] == "Yes", s[5])
        if s[4] == "No": ex("UPDATE users SET active=0 WHERE id=?", uid)
        return uid, "Added"
    if 0 in changed: raise HTTPError(400, "A login ID can't be changed; put the old one back")
    names = [None, "name", "role", "is_admin", "active", "email"]
    b = {names[f]: s[f] == "Yes" if f in (3, 4) else s[f] for f in changed}
    if password: b["password"] = password
    update_user(q1("SELECT * FROM users WHERE id=?", uid), b)
    return uid, f"Password set {datetime.now(IST):%d %b %Y}" if password else ""


def people_remove(uid):
    update_user(q1("SELECT * FROM users WHERE id=?", uid), {"active": False})


def task_load(tid=None):
    """Each task as the Tasks tab shows it: Title, Details, Assignee (login ID), Due, Client / project, Status."""
    sql = "SELECT t.*, u.username AS login FROM tasks t JOIN users u ON u.id=t.assignee_id"
    rows = q(sql + " WHERE t.id=?", tid) if tid else q(sql)
    return {r["id"]: [r["title"], r["details"], r["login"], r["due"] or "", r["client"], STATUS_LABEL[r["status"]]] for r in rows}


def task_norm(v):
    s = [cell(x) for x in v]
    s[2], s[3] = s[2].lower(), sheet_date(v[3])
    s[5] = next((label for code, label in STATUS_LABEL.items() if s[5].lower() in (code, label.lower())), s[5] or "To do")
    return s


def task_apply(tid, s, changed, _):
    names = ["title", "details", "assignee", "due", "client", "status"]
    b = {names[f]: s[f] for f in changed}
    if "assignee" in b:
        u = q1("SELECT id FROM users WHERE username=?", b["assignee"])
        if not u: raise HTTPError(400, f"No one has the login ID '{b['assignee']}'")
        b["assignee"] = u["id"]
    if "status" in b:
        b["status"] = next((c for c, label in STATUS_LABEL.items() if label == b["status"]), None)
        if not b["status"]: raise HTTPError(400, "Status must be To do, In progress, Done or Cancelled")
    f = task_fields(b, True)
    if tid is None:  # ponytail: tasks added in the sheet are recorded as created by the first admin
        return create_task(f, q1("SELECT id FROM users WHERE is_admin=1 AND active=1 ORDER BY id")["id"]), "Added"
    change_task(q1("SELECT * FROM tasks WHERE id=?", tid), f)
    return tid, ""


def task_remove(tid):
    """Deleting an open task's row cancels it. A finished one just leaves the sheet: it stays done in reports."""
    t = q1("SELECT * FROM tasks WHERE id=?", tid)
    if t["status"] in ("todo", "in_progress"): change_task(t, {"status": "cancelled"})


def task_updated(tid):
    return datetime.fromtimestamp(q1("SELECT updated_at FROM tasks WHERE id=?", tid)[0], IST).strftime("%Y-%m-%d %H:%M")


def merge_tab(tab, cols, grid, load, norm, apply, remove, extra, out, secret=False):
    """Two-way sync of People or Tasks. Columns: ID, six that sync both ways, one more (secret=True: typed in the
    sheet, used once and cleared, i.e. New password; else written by the app, i.e. Updated), Sync note.
    The app is changed row by row; what goes to the sheet is collected in out."""
    state = {r["key"]: (json.loads(r["data"]), r["gone"]) for r in q("SELECT key, data, gone FROM sheet_rows WHERE tab=?", tab)}
    app, seen = load(), set()

    def not_applied(n, raw, why):  # only the note changes (and a typed password is cleared all the same)
        out.notes.append((tab, n, raw, ["" if secret else cell(raw[7]), "Not applied: " + why]))

    for n, raw in enumerate(grid[1:], 2):  # n: the row number in the sheet
        raw = (list(raw) + [""] * len(cols))[:len(cols)]
        rid, s, note = cell(raw[0]), norm(raw[1:7]), cell(raw[8])
        typed = cell(raw[7]) if secret else ""
        if not rid and not any(cell(v) for v in raw[1:8]): continue  # an empty row
        if rid and (not rid.isdigit() or int(rid) not in app or rid in seen):
            not_applied(n, raw, "unknown or repeated ID. Clear the ID cell to add this as a new row.")
            continue
        msgs = []
        if rid:  # three-way merge, cell by cell: what the sheet has, what the app has, what they last agreed on
            seen.add(rid)
            a = app[int(rid)]
            base = state.get(rid, (s, 0))[0]  # no record (a new or swapped sheet): the app wins
            changed = {f for f in range(6) if s[f] != base[f] and a[f] == base[f] and s[f] != a[f]}
            both = [cols[f + 1] for f in range(6) if s[f] != base[f] and a[f] != base[f] and s[f] != a[f]]
            if both: msgs.append(f"Kept the app's {', '.join(both)}: it changed in both places")
            vals = [s[f] if f in changed else a[f] for f in range(6)]
        else:
            changed, vals = set(range(6)), s
        if changed or typed:
            try: new, msg = apply(int(rid) if rid else None, vals, changed, typed)
            except HTTPError as e:  # nothing of this row is applied
                not_applied(n, raw, e.msg + (" Type the password again." if typed else ""))
                continue
            rid, vals = str(new), load(new)[new]
            seen.add(rid)
            out.applied += 1
            msgs += [msg] if msg else []
        note = "; ".join(msgs) if msgs else "" if note.startswith("Not applied") else note
        want = [int(rid), *vals, extra(int(rid)), note]
        if [cell(v) for v in raw] != [cell(v) for v in want]: out.writes.append((a1(tab, f"A{n}"), [want]))
        out.saves.append((tab, rid, vals, 0))

    gone = [k for k, (_, g) in state.items() if not g and k not in seen and int(k) in app]
    if len(gone) > MAX_SHEET_DELETES:
        out.problems.append(f"{tab}: {len(gone)} rows disappeared at once, so they were put back. Remove a few at a time"
                            + (", or set Active to No." if tab == "People" else ", or set Status to Cancelled."))
    for k in gone:
        try:
            if len(gone) > MAX_SHEET_DELETES: raise HTTPError(409, "too many rows disappeared at once")
            remove(int(k))
            out.applied += 1
            out.saves.append((tab, k, load(int(k))[int(k)], 1))  # gone: stays out of the sheet until it changes in the app
        except HTTPError as e:
            out.appends.setdefault(tab, []).append([int(k), *app[int(k)], extra(int(k)), f"Put back: {e.msg}"])
    for i, a in load().items():  # in the app but not in the sheet: new, or changed since its row was deleted
        k = str(i)
        if k in seen or k in gone or k in state and state[k][1] and state[k][0] == a: continue
        out.appends.setdefault(tab, []).append([i, *a, extra(i), ""])
        out.saves.append((tab, k, a, 0))


def mirror_tab(tab, keys, rows, out):
    """A read-only copy (Entries, Day summaries). keys: the key column(s) as read from the sheet; rows: {key: row}.
    Only the keys are read, so a sorted tab is fine. Rows typed in by hand are left alone."""
    state = {r["key"]: r["data"] for r in q("SELECT key, data FROM sheet_rows WHERE tab=?", tab)}
    have = {}
    for n, raw in enumerate(keys[1:], 2):
        k = "|".join(cell(v) for v in raw)
        if k in rows: have.setdefault(k, n)
        elif k in state: out.deletes.append((tab, n))  # it's gone from the app (an entry deleted the same day)
    for k, row in rows.items():
        if k not in have: out.appends.setdefault(tab, []).append(row)
        elif state.get(k) != json.dumps(row): out.writes.append((a1(tab, f"A{have[k]}"), [row]))
        if state.get(k) != json.dumps(row) or k not in have: out.saves.append((tab, k, row, 0))
    out.drops += [(tab, k) for k in state if k not in rows]


def entry_rows():  # submitted only: drafts stay private, as in the app
    return {str(r["id"]): [r["id"], r["day"], r["username"], r["name"],
                           datetime.fromtimestamp(r["recorded_at"], IST).strftime("%Y-%m-%d %H:%M"),
                           "Slack" if r["client_id"].startswith("slack:") else "App", r["transcript"], "Yes" if r["edited_at"] else ""]
            for r in q("SELECT e.*, u.username, u.name FROM entries e JOIN users u ON u.id=e.user_id "
                       "WHERE e.status='submitted' ORDER BY e.day, e.recorded_at")}


def daysum_rows():
    return {f"{r['period']}|{r['username']}": [r["period"], r["username"], r["name"], r["text"]]
            for r in q("SELECT s.period, s.text, u.username, u.name FROM summaries s JOIN users u ON u.id=s.user_id "
                       "WHERE s.kind='day' ORDER BY s.period, u.username")}


def chunks(rows, limit=1_000_000):  # Google suggests requests under 2 MB
    part, size = [], 0
    for r in rows:
        if part and size + len(json.dumps(r)) > limit: yield part; part, size = [], 0
        part.append(r); size += len(json.dumps(r))
    if part: yield part


def sync_sheet(sheet):
    """One sync. The app's changes are made as the rows are read; what was agreed with the sheet is saved only
    after every write to Google has gone through, so a failed write is simply redone by the next sync."""
    tabs = sheet.tabs()
    layout = {"People": PEOPLE_COLS, "Tasks": TASK_COLS, "Entries": ENTRY_COLS, "Day summaries": DAYSUM_COLS}
    if missing := [t for t in layout if t not in tabs]:
        sheet.add_tabs(missing)
        tabs = sheet.tabs()
    people, tasks, e_head, e_keys, d_head, d_keys = sheet.read([
        a1("People", "A:I"), a1("Tasks", "A:I"), a1("Entries", "A1:H1"), a1("Entries", "A:A"),
        a1("Day summaries", "A1:D1"), a1("Day summaries", "A:B")])
    out = SimpleNamespace(writes=[], deletes=[], appends={}, saves=[], drops=[], notes=[], problems=[], applied=0)
    ok = {}
    for tab, head in (("People", people[:1]), ("Tasks", tasks[:1]), ("Entries", e_head), ("Day summaries", d_head)):
        got = [cell(v) for v in (head[0] if head else [])]
        ok[tab] = not any(got) or got[:len(layout[tab])] == layout[tab]
        if not any(got): out.writes.append((a1(tab, "A1"), [layout[tab]]))
        if not ok[tab]: out.problems.append(f"{tab} wasn't synced: its first row must be {', '.join(layout[tab])}")
    if ok["People"]:
        merge_tab("People", PEOPLE_COLS, people, people_load, people_norm, people_apply, people_remove, lambda i: "", out, secret=True)
    if ok["Tasks"]: merge_tab("Tasks", TASK_COLS, tasks, task_load, task_norm, task_apply, task_remove, task_updated, out)
    if ok["Entries"]: mirror_tab("Entries", e_keys, entry_rows(), out)
    if ok["Day summaries"]: mirror_tab("Day summaries", d_keys, daysum_rows(), out)
    for tab, n, raw, want in out.notes:  # rows not applied: column H (password cleared / Updated kept) and the note
        if [cell(raw[7]), cell(raw[8])] != want: out.writes.append((a1(tab, f"H{n}"), [want]))
    # Cells first (row numbers as read), then deleted rows (bottom up), then new rows at the end.
    # ponytail: if someone sorts the tab in the second between our read and write, a row can land on the wrong line
    if out.writes: sheet.write(out.writes)
    if out.deletes: sheet.delete_rows([(tabs[t], n) for t, n in out.deletes])
    for tab, rows in out.appends.items():
        for part in chunks(rows): sheet.append(tab, part)
    for tab, key, data, gone in out.saves: ex("INSERT OR REPLACE INTO sheet_rows VALUES(?,?,?,?)", tab, key, json.dumps(data), gone)
    for tab, key in out.drops: ex("DELETE FROM sheet_rows WHERE tab=? AND key=?", tab, key)
    result = f"{out.applied} change{'s' * (out.applied != 1)} from the sheet applied"
    return result + (f", {len(out.notes)} row{'s' * (len(out.notes) != 1)} not applied (see Sync note)" if out.notes else ""), out.problems


def sheet_config():
    sid = setting("sheet_id", "")
    return sid if sid and GOOGLE_KEY.is_file() else None


_sheet = None


def run_sync(_=None):
    global _sheet
    if not (sid := sheet_config()): return
    try:
        key = json.loads(GOOGLE_KEY.read_text())
        if not _sheet or (_sheet.id, _sheet.key) != (sid, key): _sheet = Sheet(sid, key)  # keeps the token between syncs
        result, problems = sync_sheet(_sheet)
        sync_status.update(at=now(), result=result, error="; ".join(problems) or None)
    except Exception as e:
        traceback.print_exc()
        sync_status.update(at=now(), error=str(e)[:500])


sheet_jobs = Jobs(run_sync)


def sheet_loop():
    while True:
        if sheet_config(): sheet_jobs.put("sync")
        time.sleep(SYNC_EVERY)


@route("GET", "/api/sheet", auth="admin")
def get_sheet(h):
    try: account = json.loads(GOOGLE_KEY.read_text()).get("client_email") if GOOGLE_KEY.is_file() else None
    except ValueError: account = None
    return {"sheet_id": setting("sheet_id", ""), "key_file": str(GOOGLE_KEY), "key_found": GOOGLE_KEY.is_file(),
            "account": account, "syncing": sheet_jobs.pending("sync"), **sync_status}


@route("PUT", "/api/sheet", auth="admin")
def put_sheet(h):
    s = str(h.body_json().get("sheet", "")).strip()
    m = re.search(r"/d/([A-Za-z0-9_-]{20,})", s) or re.fullmatch(r"([A-Za-z0-9_-]{20,})", s)
    if s and not m: raise HTTPError(400, "Paste the sheet's link from the browser's address bar")
    sid = m.group(1) if m else ""
    if sid != setting("sheet_id", ""):
        ex("DELETE FROM sheet_rows")  # what was agreed with another sheet means nothing for this one
        sync_status.update(at=None, error=None, result="")
    if sid: ex("INSERT OR REPLACE INTO settings VALUES('sheet_id', ?)", sid)
    else: ex("DELETE FROM settings WHERE key='sheet_id'")
    if sheet_config(): sheet_jobs.put("sync")


@route("POST", "/api/sheet/sync", auth="admin")
def sync_now(h):
    if not sheet_config(): raise HTTPError(409, "Set up the Google Sheet first")
    sheet_jobs.put("sync")


# ---------- Slack ----------
# People DM the bot a voice note; it becomes an entry like one recorded in the app (client_id "slack:<file id>").
# Socket Mode: this server connects out to Slack, so it needs no public address. Off unless both tokens are set,
# in data/slack.env (KEY=value lines; on the server /var/lib/timesheet/slack.env) or the environment.

SLACK_ENV = DB_PATH.parent / "slack.env"
SLACK_FILES = "files.slack.com"
slack_web, slack_bot_user = None, None  # the Web API client and the bot's own user ID, once connected


def slack_tokens():
    """(app-level token xapp-…, bot token xoxb-…)."""
    vals = {}
    if SLACK_ENV.is_file():
        for line in SLACK_ENV.read_text().splitlines():
            k, _, v = line.partition("=")
            vals[k.strip()] = v.strip()
    return tuple(os.environ.get(k) or vals.get(k, "") for k in ("SLACK_APP_TOKEN", "SLACK_BOT_TOKEN"))


class SlackRedirect(urllib.request.HTTPRedirectHandler):
    """Slack's docs: never let the bot token follow a redirect to another host."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        u = urlparse(newurl)
        if new and u.scheme == "https" and u.hostname == SLACK_FILES:
            new.add_unredirected_header("Authorization", req.unredirected_hdrs["Authorization"])
        return new


def slack_download(url, token):
    u = urlparse(url)
    if u.scheme != "https" or u.hostname != SLACK_FILES: raise ValueError(f"Not a Slack file address: {u.hostname}")
    req = urllib.request.Request(url)
    req.add_unredirected_header("Authorization", "Bearer " + token)  # unredirected: not copied onto redirects
    with urllib.request.build_opener(SlackRedirect).open(req, timeout=120) as r:
        if r.headers.get_content_type() == "text/html": raise ValueError("Slack sent a web page, not the file. Has the app the files:read scope?")
        data = r.read(MAX_AUDIO + 1)
    if len(data) > MAX_AUDIO: raise ValueError("That file is too large")
    return data


def mrkdwn(s): return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")  # Slack's escaping rules
def section(text): return {"type": "section", "text": {"type": "mrkdwn", "text": text[:3000]}}  # 3000: Slack's limit
def note_line(text): return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def slack_button(label, action, value, style=None):
    return {"type": "button", "text": {"type": "plain_text", "text": label}, "action_id": action, "value": value,
            **({"style": style} if style else {})}


def slack_person(slack_user):
    """(account or None, Slack name, Slack email). A Slack user is linked to an account once, by work email."""
    if u := q1("SELECT * FROM users WHERE slack_id=?", slack_user): return u, None, None
    info = slack_web.users_info(user=slack_user)["user"]
    name, email = info.get("real_name") or info.get("name") or slack_user, (info.get("profile") or {}).get("email", "").lower()
    if not u and email and (u := q1("SELECT * FROM users WHERE email=? AND slack_id IS NULL", email)):
        ex("UPDATE users SET slack_id=? WHERE id=?", slack_user, u["id"])
    return u, name, email


def slack_add(uid, f, channel, ts):
    """One audio file from Slack becomes an entry recorded at the message's time; the bot's reply tracks it."""
    cid = "slack:" + f["id"]
    if q1("SELECT 1 FROM entries WHERE client_id=?", cid): return  # Slack sent the same event again
    (AUDIO_DIR / cid).write_bytes(slack_download(f["url_private_download"], slack_tokens()[1]))
    recorded = min(float(ts), now())
    try: eid = ex("INSERT INTO entries(user_id, client_id, day, recorded_at, status) VALUES(?,?,?,?,'queued')",
                  uid, cid, ist_day(recorded), recorded).lastrowid
    except sqlite3.IntegrityError: return  # two copies of the event raced
    reply = slack_web.chat_postMessage(channel=channel, thread_ts=ts, text="Got it. Turning it into text…")
    ex("INSERT INTO slack_notes VALUES(?,?,?,?)", eid, channel, ts, reply["ts"])
    whisper_jobs.put(eid)


def slack_event(ev):
    """A message in someone's DM with the bot."""
    if ev.get("type") != "message" or ev.get("channel_type") != "im" or ev.get("bot_id") or ev.get("subtype") not in (None, "file_share"):
        return  # not a DM, the bot's own message, or an edit/deletion
    if not ev.get("user") or ev["user"] == slack_bot_user: return
    channel, ts = ev["channel"], ev["ts"]
    say = lambda text: slack_web.chat_postMessage(channel=channel, thread_ts=ts, text=text)
    files = [slack_web.files_info(file=f["id"])["file"] if f.get("file_access") == "check_file_info" else f for f in ev.get("files") or []]
    # ponytail: Slack doesn't document the file type of its recorded clips, so any audio or video file is tried
    audio = [f for f in files if (f.get("mimetype") or "").startswith(("audio/", "video/")) and f.get("url_private_download")]
    if not audio:
        return say(("I can only use voice notes: record an audio clip, or attach an audio file." if files
                    else "Send me a voice note about your day.") + " How to record it:\n\n" + mrkdwn(guide()))
    if any(f.get("size", 0) > MAX_AUDIO for f in audio): return say("That recording is too large (over 50 MB). Try a shorter one.")
    u, name, email = slack_person(ev["user"])
    if u and not u["active"]: return say("Your Timesheet account is deactivated, so I can't add this. Ask an admin.")
    if not u:  # kept (not downloaded) until an admin links this Slack account to a person
        for f in audio:
            ex("INSERT OR IGNORE INTO slack_inbox VALUES(?,?,?,?,?,?,?)", f["id"], ev["user"], name, email, channel, ts, json.dumps(f))
        return say(f"I couldn't find a Timesheet login with your Slack email ({email or 'none'}). Your note is kept: "
                   "an admin will link your account, and it will be added then.")
    for f in audio: slack_add(u["id"], f, channel, ts)


def slack_reply(eid, problem=None):
    """Rewrite the bot's reply to a Slack note so it shows the entry as it is now: text, summary, buttons."""
    n, e = q1("SELECT * FROM slack_notes WHERE entry_id=?", eid), q1("SELECT * FROM entries WHERE id=?", eid)
    if not (n and e and slack_web) or e["status"] == "queued": return
    if e["status"] == "failed":
        text, blocks = f"Sorry, that didn't work. {e['error']}", None
    else:
        draft = e["status"] == "draft" and e["day"] == ist_day()
        text = f"What I heard: {e['transcript']}"[:3000]
        summary = summary_text("preview", e["user_id"], e["day"]) or summary_text("day", e["user_id"], e["day"])
        blocks = [section(f"*What I heard:*\n{mrkdwn(e['transcript'])}")]
        if summary: blocks.append(section(f"*Summary of {'today' if e['day'] == ist_day() else e['day']}:*\n{mrkdwn(summary)}"))
        for s in q(f"SELECT s.task_id, t.title FROM task_suggestions s JOIN tasks t ON t.id=s.task_id WHERE s.entry_id=? "
                   f"AND s.state='pending' AND t.assignee_id=? AND t.{OPEN_TASK}", eid, e["user_id"]):
            blocks += [section(f"Looks like you finished *{mrkdwn(s['title'])}*. Mark it done?"),
                       {"type": "actions", "elements": [slack_button("Mark done", "task_done", f"{eid}:{s['task_id']}", "primary"),
                                                        slack_button("Not yet", "task_not_yet", f"{eid}:{s['task_id']}")]}]
        buttons = [slack_button("Submit", "submit", str(eid), "primary")] if draft else []
        if PUBLIC_URL:  # a link to localhost would be no use to anyone else
            buttons.append({"type": "button", "text": {"type": "plain_text", "text": "Edit in app" if draft else "Open in app"},
                            "action_id": "open", "url": PUBLIC_URL + ("/#/" if e["day"] == ist_day() else f"/#/day/{e['day']}")})
        if buttons: blocks.append({"type": "actions", "elements": buttons})
        blocks.append(note_line("Draft: check the text, then Submit. You can also fix it in the app until midnight." if draft
                                else "Submitted."))
    if problem: blocks = (blocks or [section(mrkdwn(text))]) + [note_line(f"Couldn't do that: {mrkdwn(problem)}")]
    try: slack_web.chat_update(channel=n["channel"], ts=n["reply_ts"], text=text, **({"blocks": blocks} if blocks else {}))
    except Exception: traceback.print_exc()  # Slack being down mustn't stop transcriptions or summaries


def slack_action(p):
    """A button in one of the bot's replies. Same rules as the app: own entries, today only, the person decides."""
    u = q1("SELECT * FROM users WHERE slack_id=? AND active=1", p.get("user", {}).get("id"))
    for a in p.get("actions", []):
        if not u or a.get("action_id") not in ("submit", "task_done", "task_not_yet"): continue  # "open" is a link
        eid, _, tid = a["value"].partition(":")
        problem = None
        try:
            if a["action_id"] == "submit": submit(u["id"], eid)
            else: answer_task(u["id"], eid, tid, "accept" if a["action_id"] == "task_done" else "dismiss")
        except HTTPError as err: problem = err.msg
        slack_reply(int(eid), problem)


def slack_listener(client, req):
    from slack_sdk.socket_mode.response import SocketModeResponse
    client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))  # ack first, or Slack retries
    try:
        if req.type == "events_api": slack_event(req.payload.get("event", {}))
        elif req.type == "interactive" and req.payload.get("type") == "block_actions": slack_action(req.payload)
    except Exception: traceback.print_exc()


def start_slack():
    global slack_web, slack_bot_user
    app_token, bot_token = slack_tokens()
    if not (app_token and bot_token): return
    try:
        from slack_sdk import WebClient
        from slack_sdk.socket_mode import SocketModeClient  # built in: no extra packages; reconnects by itself
        slack_web = WebClient(token=bot_token)
        slack_bot_user = slack_web.auth_test()["user_id"]
        client = SocketModeClient(app_token=app_token, web_client=slack_web)
        client.socket_mode_request_listeners.append(slack_listener)  # runs in the client's pool of 10 threads
        client.connect()
        print("Slack: connected")
    except Exception as err:
        slack_web = None
        print(f"! Slack didn't connect ({err}). Check the tokens in {SLACK_ENV}.")


@route("GET", "/api/slack", auth="admin")
def slack_status(h):
    rows = q("SELECT slack_user, name, email, COUNT(*) AS notes, MAX(CAST(ts AS REAL)) AS last FROM slack_inbox "
             "GROUP BY slack_user ORDER BY last DESC")
    return {"connected": slack_web is not None, "env_file": str(SLACK_ENV), "waiting": [dict(r) for r in rows]}


@route("POST", "/api/slack/link", auth="admin")
def slack_link(h):
    """Link a Slack account nobody matched to a person; their waiting notes are then added."""
    b = h.body_json()
    u, su = q1("SELECT * FROM users WHERE id=? AND active=1", int(b.get("user") or 0)), str(b.get("slack_user", ""))
    if not u: raise HTTPError(400, "Pick an active person")
    waiting = q("SELECT * FROM slack_inbox WHERE slack_user=?", su)
    if not waiting: raise HTTPError(404, "No notes are waiting from that Slack account")
    if u["slack_id"] and u["slack_id"] != su: raise HTTPError(409, f"{u['name']} is already linked to another Slack account")
    ex("UPDATE users SET slack_id=? WHERE id=?", su, u["id"])
    failed = 0
    for r in waiting:  # ponytail: downloaded here, in the request; a few notes take a few seconds
        try:
            slack_add(u["id"], json.loads(r["file"]), r["channel"], r["ts"])
            ex("DELETE FROM slack_inbox WHERE file_id=?", r["file_id"])
        except Exception:
            traceback.print_exc()
            failed += 1
    if failed: raise HTTPError(502, f"Linked, but {failed} note(s) couldn't be fetched from Slack. They stay in the list.")


# ---------- startup ----------

def requeue():
    """Retry anything that was mid-transcription when the server stopped (or failed while a model was missing)."""
    for e in q("SELECT id, client_id FROM entries WHERE status IN ('queued','failed')"):
        if (AUDIO_DIR / e["client_id"]).exists():
            ex("UPDATE entries SET status='queued', error=NULL WHERE id=?", e["id"])
            whisper_jobs.put(e["id"])


def preflight():
    try: import faster_whisper  # noqa: F401
    except ImportError: print("! faster-whisper isn't installed: .venv/bin/pip install -r requirements.txt")
    try:
        with urllib.request.urlopen(OLLAMA_URL + "/api/tags", timeout=3) as r:
            if not any(m["name"] in (LLM_MODEL, LLM_MODEL + ":latest") for m in json.load(r)["models"]):
                print(f"! Ollama is running but {LLM_MODEL} is missing: ollama pull {LLM_MODEL}")
    except OSError:
        print(f"! Ollama isn't reachable at {OLLAMA_URL}. Start it (server: sudo systemctl start ollama; "
              "Mac: open the Ollama app). Summaries wait until it's up.")


def selftest():
    """Load both models and run each once, so a broken setup fails here and not on the first real recording."""
    ok = True
    try:
        import numpy as np
        device, compute = whisper_settings()
        print(f"Whisper {WHISPER_MODEL} on {device} ({compute}) ...", end=" ")
        t = time.time()
        segments, _ = load_whisper().transcribe(np.zeros(16000, dtype=np.float32), vad_filter=False)
        list(segments)  # segments are lazy: this is what actually runs the model on the GPU/CPU
        print(f"ok, {time.time() - t:.0f}s including loading")
        if device == "cpu": print("  No GPU found: everything runs on the processor and is several times slower.")
    except Exception as e:
        ok = False
        print(f"FAILED: {e}")
    try:
        print(f"Summaries with {LLM_MODEL} via Ollama ...", end=" ")
        t = time.time()
        llm("Reply with the single word OK.", "Say OK.")
        print(f"ok, {time.time() - t:.0f}s")
    except Exception as e:
        ok = False
        print(f"FAILED: {e}")
    return ok


def backup(dest):
    """Copy the database into dest (safe while the app is running) and keep the newest 30 copies."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / f"timesheet-{datetime.now(IST):%Y-%m-%d}.db"
    copy_db(out)
    for old in sorted(dest.glob("timesheet-*.db"))[:-30]: old.unlink()
    print(f"Backed up to {out}")


def adduser(argv):
    p = argparse.ArgumentParser(prog="app.py adduser")
    p.add_argument("username"); p.add_argument("name")
    p.add_argument("--role", default=""); p.add_argument("--admin", action="store_true"); p.add_argument("--email")
    a = p.parse_args(argv)
    pw = getpass.getpass("Password: ")
    if pw != getpass.getpass("Same password again: "): sys.exit("Passwords don't match")
    try: create_user(a.username, a.name, pw, a.role, a.admin, a.email)
    except HTTPError as e: sys.exit(e.msg)
    print(f"Created {'admin' if a.admin else 'user'} '{a.username.lower()}'")


def main():
    sys.stdout.reconfigure(line_buffering=True)  # show startup messages immediately, even when not in a terminal
    init_db()
    cmd, args = sys.argv[1:2], sys.argv[2:]
    if cmd == ["adduser"]: return adduser(args)
    if cmd == ["selftest"]: sys.exit(0 if selftest() else 1)
    if cmd == ["backup"]: return backup(args[0] if args else DB_PATH.parent / "backups")
    preflight()
    requeue()
    whisper_jobs.start(); llm_jobs.start(); sheet_jobs.start()
    start_slack()  # does nothing without the Slack tokens
    threading.Thread(target=sheet_loop, daemon=True).start()  # does nothing until a Google Sheet is set up
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Timesheet is running on http://{HOST}:{PORT}" + (f", public address {PUBLIC_URL}" if PUBLIC_URL else ""))
    if not q1("SELECT 1 FROM users WHERE is_admin=1"):
        print('No admin yet. Create one with:  adduser admin "Your Name" --admin   (see README)')
    try: server.serve_forever()
    except KeyboardInterrupt: pass


if __name__ == "__main__":
    main()
