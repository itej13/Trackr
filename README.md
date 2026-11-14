# Timesheet (v2, online)

Employees record a voice note about their day, in Hindi, English or both. The server turns it into English text; they check it, fix anything misheard, and submit. Admins see each person's transcripts, a summary of each day, a month calendar per person, and monthly summaries per person and for the whole company.

Everything runs on one server you control. Speech-to-text is Whisper large-v3 (faster-whisper) and summaries come from Qwen 2.5 14B via Ollama, both on that server, so no transcript goes to an outside AI service. Data is a SQLite file. Caddy in front provides HTTPS with a trusted certificate.

Version 1 (fully offline, on one Mac) is the git tag `v1.0`.

```
 Phones / laptops (installable web app; records with no signal, uploads later)
          │ HTTPS
          ▼
 Server:  Caddy ──► app.py ──► SQLite
                      ├─► Whisper large-v3   (NVIDIA GPU if there is one, else CPU)
                      └─► Ollama ─► Qwen 2.5 14B
          nightly: database copy kept 30 days
```

## Deploy to a server

You need an Ubuntu 22.04 or 24.04 server you can reach over SSH. Ideally it has an NVIDIA GPU with at least 20 GB of memory (for example an RTX 4090, L4 or A10). Without a GPU it still works but is several times slower; give it at least 24 GB of RAM.

```bash
sudo git clone https://github.com/itej13/voice-timesheet.git /opt/timesheet
sudo /opt/timesheet/deploy/setup.sh
sudo /opt/timesheet/deploy/ts adduser admin "Your Name" --admin --role Founder
```

The setup script installs everything, downloads both models (~12 GB), sets up auto-restart, nightly backups and a firewall, then runs a self-test of both models. It prints the address, for example `https://203-0-113-7.sslip.io`. That's a free name pointing at the server's IP, so HTTPS works without buying a domain.

**Your own domain:** point a DNS A record (for example `timesheet.yourcompany.com`) at the server, then run `sudo /opt/timesheet/deploy/setup.sh timesheet.yourcompany.com`. If your provider has its own firewall (Hostinger has one in hPanel), allow ports 80 and 443 there too.

**Update** to the latest code:

```bash
cd /opt/timesheet && sudo git pull && sudo deploy/setup.sh
```

**Everyday commands**

| What | Command |
|---|---|
| Add a person (or use **Team** in the app) | `sudo /opt/timesheet/deploy/ts adduser riya "Riya Sharma" --role "Video editor"` |
| Check both models work | `sudo /opt/timesheet/deploy/ts selftest` |
| Live log | `journalctl -u timesheet -f` |
| Restart | `sudo systemctl restart timesheet` |
| Backups | `/var/lib/timesheet/backups/`, one per night, newest 30 kept |
| Uptime monitor URL | `https://<address>/api/health` |

## Phones

Open the address and choose **Add to Home Screen** (iPhone, from the Share menu) or **Install app** (Android Chrome). Recording works with no signal: the note is kept on the phone and uploads by itself once there's a connection.

## Run it on your own computer (development)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
ollama pull qwen2.5:14b
.venv/bin/python -c "from faster_whisper import download_model; download_model('large-v3')"
.venv/bin/python app.py adduser admin "Your Name" --admin
.venv/bin/python app.py                      # http://localhost:8000
```

## How it behaves

- **Editing:** today's entries can be edited, re-recorded or deleted until midnight IST. Older days are read-only. A draft left unsubmitted at midnight, or a recording made yesterday that uploads today, is submitted automatically, so nothing said is lost.
- **Edits are visible:** when someone edits an already-submitted entry, admins see "edited" and the text from before the edit.
- **Audio is deleted** as soon as it's been transcribed. Only text is stored.
- **Comments** are visible to admins only.
- **Summaries:** day summaries are written automatically. Monthly and company summaries are written when an admin clicks Generate or Refresh.
- **Glossary** (admin): tool names, client names and jargon. Whisper uses it to spell them right, so add your clients' names here.
- **Security:** the app and Ollama only listen inside the server, and the firewall allows only SSH and web traffic. Passwords need at least 10 characters. Logins lock for 15 minutes after 5 wrong passwords for one ID, or 50 from one address. Pages are served with strict browser security headers.

## Settings

`deploy/setup.sh` writes these to `/etc/timesheet.env`.

| Variable | Default | |
|---|---|---|
| `TS_HOST`, `TS_PORT` | `127.0.0.1`, `8000` | Where the app listens; Caddy forwards to it |
| `TS_PUBLIC_URL` | unset | The public `https://` address. Turns on secure cookies and reads the visitor's IP from Caddy |
| `TS_DB` | `data/timesheet.db` | On the server: `/var/lib/timesheet/timesheet.db` |
| `TS_WHISPER` | `large-v3` | Don't use `large-v3-turbo`: it can't translate Hindi to English |
| `TS_WHISPER_DEVICE` | NVIDIA GPU if found, Apple GPU on a Mac, else `cpu` | |
| `TS_WHISPER_COMPUTE` | `float16` on a GPU, `int8` on CPU | |
| `TS_LLM` | `qwen2.5:14b` | Any Ollama model. `qwen2.5:7b` is twice as fast, but in tests its monthly summaries listed tools as clients and missed open blockers |
| `TS_OLLAMA` | `http://127.0.0.1:11434` | |

## Check

```bash
.venv/bin/python test_app.py
```

This covers access rules, drafts, the midnight lock, late uploads, edits, summaries, the login throttle and security headers, with the models faked.

## Known limits

- **Day and per-person monthly summaries are reliable** in tests: right clients and tools, and work redone later isn't reported as still open.
- **The company summary is an overview, not a ledger.** In a 50-person test it kept every client and every one-off problem, but per-client counts were made up and one blocker was put under the wrong client. Check numbers in each person's monthly summary.
- The company summary is one background job that first writes any missing monthly summaries. For 40–50 people on a GPU expect about 10 minutes, and day summaries wait while it runs.
- **Without a GPU** everything still works, but transcripts and summaries take minutes instead of seconds, and an evening rush builds a queue.
- **Backups stay on the same server.** That protects against mistakes, not against losing the server. Copy `/var/lib/timesheet/backups` somewhere else, or turn on your provider's server backups.
- No password reset by email. An admin sets a new password under **Team → Edit**.
