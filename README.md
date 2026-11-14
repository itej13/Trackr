# Timesheet (v1, fully offline)

Employees record a voice note about their day, in Hindi, English or both. It's turned into English text on this machine; they check it, fix anything misheard, and submit. Admins see each person's transcripts, a summary of each day, a month calendar per person, and monthly summaries per person and for the whole company.

Nothing leaves the machine. Speech-to-text is Whisper large-v3 (faster-whisper), summaries come from Qwen 2.5 14B via Ollama, and data is a SQLite file.

## One-time setup (the only step that needs internet)

```bash
cd timesheet
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
ollama pull qwen2.5:14b                                                                     # ~9 GB
.venv/bin/python -c "from faster_whisper import WhisperModel; WhisperModel('large-v3')"   # ~3 GB, skip if cached
.venv/bin/python app.py adduser admin "Your Name" --admin --role Founder                    # asks for a password
```

## Run

Start Ollama (open the Ollama app, or `ollama serve`), then:

```bash
caffeinate -is .venv/bin/python app.py
```

`caffeinate` stops the Mac from sleeping while the server runs. A sleeping server is unreachable from phones (recordings wait on the phone) and pauses transcription and summaries. Keep it on the charger with the lid open, or set it to never sleep on power in System Settings → Battery → Options.

Open http://localhost:8000. The admin adds everyone else under **Team**: name, login ID, password, role.

## Phones

Phones use the app over the office Wi-Fi. The network doesn't need internet, but the phone and this machine must be on the same network. Browsers only allow the microphone over HTTPS, so do this once:

```bash
./make-cert.sh
TS_CERT=data/cert/server.crt TS_KEY=data/cert/server.key .venv/bin/python app.py
```

On each phone, open `https://<this machine's IP>:8000/ca.crt` (the server prints the address; accept the warning this one time) and trust the certificate:

- **iPhone:** Settings → General → VPN & Device Management → install the profile. Then Settings → General → About → Certificate Trust Settings → turn on "Timesheet local CA".
- **Android:** Settings → Security → More security settings → Encryption & credentials → Install a certificate → CA certificate.

Then open `https://<IP>:8000` and choose **Add to Home Screen** (iPhone, from the Share menu) or **Install app** (Android Chrome). Give this machine a fixed address on the office network (a DHCP reservation on the router). If its IP changes anyway, run `./make-cert.sh` again and restart. Phones keep trusting the same CA.

## How it behaves

- **Recording works without a connection.** It's saved on the phone and uploads by itself once the server is reachable. The recording's own time decides which day it belongs to.
- **Editing:** today's entries can be edited, re-recorded or deleted until midnight IST. Older days are read-only. A draft left unsubmitted at midnight, or a recording made yesterday that uploads today, is submitted automatically, so nothing said is lost.
- **Edits are visible:** when someone edits an already-submitted entry, admins see "edited" and the text from before the edit.
- **Audio is deleted** as soon as it's been transcribed. Only text is stored.
- **Comments** are visible to admins only.
- **Summaries:** day summaries are written automatically. Monthly and company summaries are written when an admin clicks Generate or Refresh.
- **Glossary** (admin): tool names, client names and jargon. Whisper uses it to spell them right, so add your clients' names here.

## Settings (environment variables)

| Variable | Default | |
|---|---|---|
| `TS_PORT` | `8000` | |
| `TS_DB` | `data/timesheet.db` | Audio waiting to be transcribed sits next to it in `audio/` |
| `TS_WHISPER` | `large-v3` | Don't use `large-v3-turbo`: it can't translate Hindi to English |
| `TS_WHISPER_DEVICE` | `mps` on Apple Silicon, else `auto` | `cpu` if the Apple GPU backend misbehaves; `cuda` on NVIDIA |
| `TS_WHISPER_COMPUTE` | `float16` on GPU, `int8` on CPU | |
| `TS_LLM` | `qwen2.5:14b` | Any Ollama model. `qwen2.5:7b` is twice as fast, but in tests its monthly summaries listed tools as clients and missed open blockers |
| `TS_OLLAMA` | `http://127.0.0.1:11434` | |
| `TS_CERT`, `TS_KEY` | unset | Set both to serve HTTPS |

## Check

```bash
.venv/bin/python test_app.py
```

This covers access rules, drafts, the midnight lock, late uploads, edits and summaries, with the models faked. Back up by copying `data/timesheet.db` while the server is stopped.

## Known limits of v1

- One machine does everything. With 40–50 people (~100 recordings a day) an M-series Mac keeps up: a 1-minute note takes about 20 s to transcribe on the Apple GPU.
- **Day and per-person monthly summaries are reliable** in tests: right clients and tools, and work redone later isn't reported as still open. Expect about 3 s per day summary and 8 s per monthly summary.
- **The company summary is an overview, not a ledger.** In a 50-person test it kept every client and every one-off problem, but per-client counts were made up and one blocker was put under the wrong client. Check numbers in each person's monthly summary. Exact per-client totals would need structured data per entry; that's a v2 change.
- The company summary is one background job that first writes any missing monthly summaries. For 40–50 people expect about 8–10 minutes, and day summaries wait while it runs.
- No password reset by email (no internet by design). An admin sets a new password under **Team → Edit**.
