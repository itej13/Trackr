#!/usr/bin/env python3
"""Timesheet v1: runs fully offline on one machine.

Speech-to-text: faster-whisper (local). Summaries: Ollama (local). Storage: SQLite. No internet at runtime.

  .venv/bin/python app.py                                  # serve on :8000
  .venv/bin/python app.py adduser admin "Your Name" --admin    # create a login (asks for the password)
"""
import argparse, getpass, hashlib, hmac, json, os, queue, re, secrets, socket, sqlite3, ssl, sys, threading, time, traceback
import urllib.error, urllib.request
from datetime import datetime, timedelta, timezone
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

os.environ.setdefault("HF_HUB_OFFLINE", "1")  # Whisper loads from the local cache only, never the network

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
DB_PATH = Path(os.environ.get("TS_DB", ROOT / "data" / "timesheet.db"))
AUDIO_DIR = DB_PATH.parent / "audio"
CA_CERT = ROOT / "data" / "cert" / "ca.crt"
PORT = int(os.environ.get("TS_PORT", "8000"))
CERT, KEY = os.environ.get("TS_CERT"), os.environ.get("TS_KEY")
WHISPER_MODEL = os.environ.get("TS_WHISPER", "large-v3")  # not large-v3-turbo: turbo can't translate
# ponytail: Apple GPU ("mps", ~3x faster than CPU on an M4 Pro) is still marked experimental in CTranslate2;
# set TS_WHISPER_DEVICE=cpu if transcriptions start failing. On a Linux box with an NVIDIA GPU: cuda + float16.
_APPLE = sys.platform == "darwin" and os.uname().machine == "arm64"
WHISPER_DEVICE = os.environ.get("TS_WHISPER_DEVICE", "mps" if _APPLE else "auto")
WHISPER_COMPUTE = os.environ.get("TS_WHISPER_COMPUTE", "float16" if WHISPER_DEVICE in ("mps", "cuda") else "int8")
LLM_MODEL = os.environ.get("TS_LLM", "qwen2.5:14b")  # 7B called tools "clients" and missed blockers in monthly summaries
OLLAMA_URL = os.environ.get("TS_OLLAMA", "http://127.0.0.1:11434")
IST = timezone(timedelta(hours=5, minutes=30))  # ponytail: IST has no DST, so a fixed offset is exact
MAX_AUDIO = 50 * 1024 * 1024
SESSION_SECS = 30 * 86400

DEFAULT_GLOSSARY = (
    "Midjourney, Runway, Kling, Veo, Sora, Pika, Luma, Higgsfield, Flux, Stable Diffusion, ComfyUI, LoRA, "
    "ElevenLabs, Suno, HeyGen, CapCut, Premiere Pro, After Effects, DaVinci Resolve, Photoshop, Illustrator, "
    "Canva, Figma, ChatGPT, Claude, Gemini, prompt, render, upscale, inpainting, lip-sync, voiceover, storyboard, "
    "UGC, reel, thumbnail, hook, CTA, A/B variant, client brief, revision, deliverable")

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
"""

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
    db().executescript(SCHEMA)


def now(): return time.time()
def ist_day(ts=None): return datetime.fromtimestamp(now() if ts is None else ts, IST).strftime("%Y-%m-%d")


class HTTPError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


def valid_day(s):
    if not s: return ist_day()
    try: return datetime.strptime(s, "%Y-%m-%d").strftime("%Y-%m-%d")
    except (TypeError, ValueError): raise HTTPError(400, "Bad date")


def valid_month(s):
    if not s: return ist_day()[:7]
    try: return datetime.strptime(s, "%Y-%m").strftime("%Y-%m")
    except (TypeError, ValueError): raise HTTPError(400, "Bad month")


# ---------- accounts ----------

def hash_pw(pw, salt=None):
    salt = salt or secrets.token_bytes(16)
    return salt.hex() + "$" + hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1).hex()


def check_pw(pw, stored):
    return hmac.compare_digest(hash_pw(pw, bytes.fromhex(stored.split("$")[0])), stored)


DUMMY_PW = hash_pw("x")  # checked for unknown login IDs, so response time doesn't reveal which IDs exist
_failures = {}  # ponytail: in-memory login throttle, resets on restart


def throttled(username):
    _failures[username] = [t for t in _failures.get(username, []) if t > now() - 900]
    return len(_failures[username]) >= 5


def token_hash(tok): return hashlib.sha256(tok.encode()).hexdigest()


def clean_name(s):
    s = str(s or "").strip()
    if not 0 < len(s) <= 80: raise HTTPError(400, "Name is required (max 80 characters)")
    return s


def clean_pw(s):
    s = str(s or "")
    if len(s) < 6: raise HTTPError(400, "Password must be at least 6 characters")
    return s


def create_user(username, name, password, role="", is_admin=False):
    username = str(username or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9._-]{2,32}", username):
        raise HTTPError(400, "Login ID: 2-32 characters, letters, numbers, . _ -")
    try:
        return ex("INSERT INTO users(username, name, role, pw, is_admin, created) VALUES(?,?,?,?,?,?)",
                  username, clean_name(name), str(role or "").strip()[:60], hash_pw(clean_pw(password)),
                  int(bool(is_admin)), ist_day()).lastrowid
    except sqlite3.IntegrityError:
        raise HTTPError(409, "That login ID is already taken")


def user_json(u):
    return {"id": u["id"], "username": u["username"], "name": u["name"], "role": u["role"],
            "is_admin": bool(u["is_admin"]), "active": bool(u["active"]), "created": u["created"]}


def glossary():
    r = q1("SELECT value FROM settings WHERE key='glossary'")
    return r["value"] if r else DEFAULT_GLOSSARY


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


def run_whisper(path):
    """Speech in Hindi, English or Hinglish -> English text."""
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel
        _whisper = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE,
                                cpu_threads=min(8, os.cpu_count() or 4), local_files_only=True)
    # ponytail: Whisper only reads the last ~220 tokens of the prompt, so a very long glossary loses its start
    segments, _ = _whisper.transcribe(str(path), task="translate", initial_prompt="Glossary: " + glossary(), vad_filter=True)
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
        return  # audio is kept, so the next server start retries it
    audio.unlink(missing_ok=True)  # audio isn't kept once we have the text
    if not text:
        ex("UPDATE entries SET status='failed', error='No speech detected. Discard it and record again.' WHERE id=?", eid)
        return
    # A past day is locked, so it can't be reviewed any more (recorded offline, uploaded late): keep it as said.
    ex("UPDATE entries SET status=?, transcript=? WHERE id=?", "draft" if e["day"] == ist_day() else "submitted", text, eid)
    touch(e["user_id"], e["day"])


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
    if kind == "month":
        days = [r[0] for r in q("SELECT DISTINCT day FROM entries WHERE user_id=? AND day LIKE ? AND status='submitted' "
                                "ORDER BY day", uid, period + "-%")]
        if refresh:
            for d in days: build("day", uid, d)
        return [f"{d}:\n{summary_text('day', uid, d)}" for d in days]
    people = q("SELECT id, name, role FROM users WHERE id IN "
               "(SELECT user_id FROM entries WHERE day LIKE ? AND status='submitted') ORDER BY name", period + "-%")
    if refresh:  # ponytail: one long job for the whole team; split per person if it ever blocks day summaries too long
        for p in people: build("month", p["id"], period)
    return [f"{p['name']} ({p['role'] or 'role not set'}):\n{summary_text('month', p['id'], period)}" for p in people]


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
    if kind == "company":
        header = f"Month: {period}\n\n"
    else:
        u = q1("SELECT name, role FROM users WHERE id=?", uid)
        header = f"Employee: {u['name']} ({u['role'] or 'role not set'})\n{'Month' if kind == 'month' else 'Date'}: {period}\n\n"
    text = llm(PROMPTS[kind], header + "\n\n".join(src))
    ex("INSERT OR REPLACE INTO summaries VALUES(?,?,?,?,?,?)", kind, uid, period, text, h, now())


llm_errors = {}


def run_build(key):
    try:
        build(*key)
        llm_errors.pop(key, None)
    except Exception as e:
        llm_errors[key] = f"Summary failed ({e}). Is Ollama running with {LLM_MODEL}?"[:300]
        raise


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
        ".json": "application/manifest+json", ".png": "image/png", ".crt": "application/x-x509-ca-cert"}


def cookie(value, max_age):
    return ("Set-Cookie", f"sid={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}" + ("; Secure" if CERT else ""))


class Handler(BaseHTTPRequestHandler):
    def do_GET(self): self.handle_any("GET")
    def do_HEAD(self): self.handle_any("GET")  # same as GET; reply() and static() skip the body
    def do_POST(self): self.handle_any("POST")
    def do_PUT(self): self.handle_any("PUT")
    def do_PATCH(self): self.handle_any("PATCH")
    def do_DELETE(self): self.handle_any("DELETE")

    def log_request(self, code="-", size="-"):
        if not isinstance(code, int) or code >= 400: super().log_request(code, size)

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
        f = CA_CERT if path == "/ca.crt" else (STATIC / (path.lstrip("/") or "index.html")).resolve()
        if not f.is_file() or (f != CA_CERT and STATIC not in f.parents):
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


def own_entry(h, eid, today_only=True):
    e = q1("SELECT * FROM entries WHERE id=? AND user_id=?", int(eid), h.user["id"])
    if not e: raise HTTPError(404, "Entry not found")
    if e["status"] == "queued": raise HTTPError(409, "Still transcribing. Try again in a moment.")
    if today_only and e["day"] != ist_day(): raise HTTPError(403, "Only today's entries can be changed")
    return e


def entry_json(e, owner):
    d = {"id": e["id"], "status": e["status"], "transcript": e["transcript"], "recorded_at": e["recorded_at"],
         "edited": e["edited_at"] is not None}
    if owner: d["error"] = e["error"]
    else: d["original"] = e["original"]  # what the admin may have read before the employee edited it
    return d


@route("POST", "/api/login", auth=None)
def login(h):
    b = h.body_json()
    username, pw = str(b.get("username", "")).strip().lower(), str(b.get("password", ""))
    if throttled(username): raise HTTPError(429, "Too many attempts. Try again in 15 minutes.")
    u = q1("SELECT * FROM users WHERE username=? AND active=1", username)
    if not check_pw(pw, u["pw"] if u else DUMMY_PW) or not u:
        _failures.setdefault(username, []).append(now())
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
    e = own_entry(h, eid)
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
def submit_entry(h, eid):
    e = own_entry(h, eid)
    if e["status"] != "draft": raise HTTPError(409, "This entry isn't a draft")
    ex("UPDATE entries SET status='submitted' WHERE id=?", e["id"])
    touch(e["user_id"], e["day"])


@route("DELETE", r"/api/entries/(\d+)")
def delete_entry(h, eid):
    e = own_entry(h, eid, today_only=False)
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


@route("POST", "/api/summaries", auth="admin")
def generate_summary(h):
    b = h.body_json()
    m, uid = valid_month(b.get("month")), int(b.get("user") or 0)
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
    return {"id": create_user(b.get("username"), b.get("name"), b.get("password"), b.get("role"), b.get("is_admin"))}


@route("PATCH", r"/api/users/(\d+)", auth="admin")
def edit_user(h, uid):
    u, b = target_user(h, uid), h.body_json()
    if u["id"] == h.user["id"] and (b.get("active") is False or b.get("is_admin") is False):
        raise HTTPError(400, "You can't deactivate yourself or remove your own admin access")
    name = clean_name(b["name"]) if "name" in b else u["name"]
    pw = hash_pw(clean_pw(b["password"])) if b.get("password") else u["pw"]
    role = str(b["role"] or "").strip()[:60] if "role" in b else u["role"]
    is_admin = int(bool(b["is_admin"])) if "is_admin" in b else u["is_admin"]
    active = int(bool(b["active"])) if "active" in b else u["active"]
    ex("UPDATE users SET name=?, role=?, pw=?, is_admin=?, active=? WHERE id=?", name, role, pw, is_admin, active, u["id"])
    if b.get("password") or not active:  # new password or deactivated: log them out everywhere (except this browser)
        ex("DELETE FROM sessions WHERE user_id=? AND token<>?", u["id"], token_hash(h.token or ""))


@route("POST", "/api/comments", auth="admin")
def add_comment(h):
    b = h.body_json()
    u, day, text = target_user(h, b.get("user")), valid_day(b.get("date")), str(b.get("text", "")).strip()
    if not 0 < len(text) <= 2000: raise HTTPError(400, "A comment must be 1-2000 characters")
    ex("INSERT INTO comments(user_id, day, author_id, text, created_at) VALUES(?,?,?,?,?)",
       u["id"], day, h.user["id"], text, now())


@route("GET", "/api/glossary", auth="admin")
def get_glossary(h): return {"text": glossary()}


@route("PUT", "/api/glossary", auth="admin")
def put_glossary(h):
    terms = [t.strip() for t in re.split(r"[,\n]", str(h.body_json().get("text", ""))) if t.strip()]
    ex("INSERT OR REPLACE INTO settings VALUES('glossary', ?)", ", ".join(terms)[:3000])


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
        print(f"! Ollama isn't reachable at {OLLAMA_URL}. Open the Ollama app (or run: ollama serve). "
              "Summaries wait until it's up.")


def lan_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))  # UDP connect sends nothing; it just picks the outgoing interface
            return s.getsockname()[0]
    except OSError:
        return None


def adduser(argv):
    p = argparse.ArgumentParser(prog="app.py adduser")
    p.add_argument("username"); p.add_argument("name")
    p.add_argument("--role", default=""); p.add_argument("--admin", action="store_true")
    a = p.parse_args(argv)
    pw = getpass.getpass("Password: ")
    if pw != getpass.getpass("Same password again: "): sys.exit("Passwords don't match")
    try: create_user(a.username, a.name, pw, a.role, a.admin)
    except HTTPError as e: sys.exit(e.msg)
    print(f"Created {'admin' if a.admin else 'user'} '{a.username.lower()}'")


def main():
    sys.stdout.reconfigure(line_buffering=True)  # show startup messages immediately, even when not in a terminal
    init_db()
    if sys.argv[1:2] == ["adduser"]: return adduser(sys.argv[2:])
    preflight()
    requeue()
    whisper_jobs.start(); llm_jobs.start()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    scheme = "http"
    if CERT:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(CERT, KEY)
        server.socket, scheme = ctx.wrap_socket(server.socket, server_side=True), "https"
    ip = lan_ip()
    print(f"Timesheet is running: {scheme}://localhost:{PORT}" + (f"   (phones on this network: {scheme}://{ip}:{PORT})" if ip else ""))
    if not q1("SELECT 1 FROM users WHERE is_admin=1"):
        print('No admin yet. Create one:  .venv/bin/python app.py adduser admin "Your Name" --admin')
    try: server.serve_forever()
    except KeyboardInterrupt: pass


if __name__ == "__main__":
    main()
