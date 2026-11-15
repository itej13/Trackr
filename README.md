# Timesheet (v2, online)

Employees record a voice note about their day, in Hindi, English or both. The server turns it into English text; they check it, fix anything misheard, and submit. Admins see each person's transcripts, a summary of each day, a month calendar per person, and monthly summaries per person and for the whole company.

Everything runs on one server you control. Speech-to-text is Whisper large-v3 (faster-whisper) and summaries come from Qwen 2.5 14B via Ollama, both on that server, so no transcript goes to an outside AI service. Data is a SQLite file. Two optional extras change what leaves the server: the **Google Sheet** keeps a copy of people, tasks, transcripts and day summaries in your Google account, and voice notes sent through **Slack** stay in Slack too. Caddy in front provides HTTPS with a trusted certificate.

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
- **Settings** (admin): the **glossary** of tool names, client names and jargon (Whisper uses it to spell them right, so add your clients' names here), and the **speaking guide** shown on top of everyone's Today screen.
- **Tasks:** admins give tasks to people (Tasks page); each person sees theirs under **My tasks** and taps Start or Done. After a voice note, the AI checks it against that person's open tasks and may ask "Looks like you finished X. Mark it done?". Nothing changes until they tap Mark done.
- **Reports** (admin): any date range up to 92 days, one person or everyone: who logged which days, each day's summary and exact words, and a summary of the range on request.
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

## Google Sheet (optional)

A Google Sheet can be the admins' control panel. **People** and **Tasks** sync both ways about once a minute: edit a name, role, Admin/Active, work email, or a task's fields there, or add rows. **Entries** and **Day summaries** are read-only copies. The app keeps working when Google is down.

- **Passwords:** type one in **New password**; the app sets it at the next sync, clears the cell and writes "Password set" in **Sync note**. Google's version history still keeps what was typed, so share the sheet with admins only.
- **Deleting a row** deactivates that person or cancels that open task. More than 5 rows gone from a tab at once is treated as an accident, and they're put back.
- **A row the app can't use** (unknown login ID, bad date, password under 10 characters, …) is skipped as a whole, with the reason in **Sync note**. If the app and the sheet changed the same cell between syncs, the app's value wins and Sync note says so.

**Set it up** (once, about 10 minutes):

1. Create an empty Google Sheet, in a company Google account if you have one. Share it only with admins.
2. Open https://console.cloud.google.com, create a project (e.g. "timesheet-sync"), then **APIs & Services → Library → Google Sheets API → Enable**.
3. **IAM & Admin → Service accounts → Create service account** (e.g. "timesheet-sync"). It needs no roles: skip that step.
4. Open the new service account, **Keys → Add key → Create new key → JSON**. A file downloads. If Google refuses, your Google Workspace organisation blocks key creation: organisations created on or after 3 May 2024 do by default (`iam.disableServiceAccountKeyCreation`). Someone with the *Organization Policy Administrator* role can allow it for this one project.
5. In the sheet, **Share** it with the service account's email (`…@….iam.gserviceaccount.com`, also shown in the app) as **Editor**.
6. Put the key on the server, readable by the app only, then delete the downloaded copy:
   ```bash
   sudo install -o root -g timesheet -m 640 key.json /var/lib/timesheet/google-key.json
   ```
   On your own computer it goes in `data/google-key.json`. Never commit it.
7. In the app: **Settings → Google Sheet**, paste the sheet's link, **Save**. The tabs are created and filled within a minute.

## Slack (optional)

People can send their update as a voice note in a direct message to the **Timesheet** bot: a Slack audio clip or an audio file. It goes through the same steps as a recording in the app. The bot answers in the thread with what it heard, the summary and a **Submit** button, plus "Looks like you finished X. Mark it done?" when that applies. In the app the entry shows "via Slack". A message without audio gets the speaking guide back.

- **Who is who:** a Slack account is matched to a person by **work email** (Team → Edit). If nobody matches, the note waits under **Team → Slack notes waiting for a person** until an admin links that Slack account. Such notes aren't downloaded until then.
- **Same rules as the app:** today's notes can be submitted until midnight; a note from an earlier day goes in as submitted; the audio is deleted from our server once it's text. It stays in Slack under your workspace's file retention settings.
- **No public address needed:** the server connects out to Slack (Socket Mode), so it also works on a laptop.

**Set it up** (a Slack workspace owner or admin, about 10 minutes):

1. Open https://api.slack.com/apps?new_app=1, choose to create the app **from a manifest**, pick your workspace, paste this, then **Next → Create**:
   ```yaml
   display_information:
     name: Timesheet
     description: Send a voice note about your day; it goes into your timesheet.
   features:
     app_home:
       home_tab_enabled: false
       messages_tab_enabled: true
       messages_tab_read_only_enabled: false
     bot_user:
       display_name: Timesheet
       always_online: true
   oauth_config:
     scopes:
       bot: [chat:write, im:history, files:read, users:read, users:read.email]
   settings:
     event_subscriptions:
       bot_events: [message.im]
     interactivity:
       is_enabled: true
     org_deploy_enabled: false
     socket_mode_enabled: true
     token_rotation_enabled: false
   ```
2. **Basic Information → App-Level Tokens → Generate Token and Scopes**: any name, scope `connections:write`. Copy the `xapp-…` token.
3. **Install App → Install to Workspace → Allow**. Then **OAuth & Permissions**: copy the **Bot User OAuth Token** (`xoxb-…`).
4. Put both tokens on the server, readable by the app only, and restart:
   ```bash
   sudo install -o root -g timesheet -m 640 /dev/null /var/lib/timesheet/slack.env
   sudo nano /var/lib/timesheet/slack.env
   ```
   Write two lines, `SLACK_APP_TOKEN=xapp-…` and `SLACK_BOT_TOKEN=xoxb-…`, save, then:
   ```bash
   sudo systemctl restart timesheet
   ```
   On your own computer they go in `data/slack.env`. Never commit them or paste them into a chat.
5. In the app, **Settings → Slack** should say *Connected*. Fill in everyone's **work email** on the Team page, then send the bot "hi" in Slack: it replies with the speaking guide.

## Check

```bash
.venv/bin/python test_app.py
```

This covers access rules, drafts, the midnight lock, late uploads, edits, summaries, reports, tasks and the "task done?" check, the Google Sheet sync and the Slack bot (against stand-ins for Google and Slack), database upgrades, the login throttle and security headers, with the models faked.

## Known limits

- **Day and per-person monthly summaries are reliable** in tests: right clients and tools, and work redone later isn't reported as still open.
- **The company summary is an overview, not a ledger.** In a 50-person test it kept every client and every one-off problem, but per-client counts were made up and one blocker was put under the wrong client. Check numbers in each person's monthly summary.
- The company summary is one background job that first writes any missing monthly summaries. For 40–50 people on a GPU expect about 10 minutes, and day summaries wait while it runs.
- **Without a GPU** everything still works, but transcripts and summaries take minutes instead of seconds, and an evening rush builds a queue.
- **Backups stay on the same server.** That protects against mistakes, not against losing the server. Copy `/var/lib/timesheet/backups` somewhere else, or turn on your provider's server backups.
- No password reset by email. An admin sets a new password under **Team → Edit**.
