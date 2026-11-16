# Timesheet (v3)

Employees record a voice note about their day, in Hindi, English or both, in the app or as a Slack message. The server turns it into English text and a short summary; they check it and submit. Admins see each person's days, summaries by day, month or any date range, tasks, and comments.

Everything runs on one server you control. Speech-to-text (Whisper large-v3) and summaries (Gemma 4 12B via Ollama) both run on that server, so no transcript goes to an outside AI service. Data is one SQLite file. Two optional extras change what leaves the server: the **Google Sheet** keeps a copy of people, tasks, transcripts and day summaries in your Google account, and voice notes sent through **Slack** stay in Slack too.

This repo (Trackr) is the working tool; the tags `v1.0`, `v2.0` and `v3.0` mark each version. Version 1, fully offline on one Mac, also lives on as the separate baseline repo `itej13/voice-timesheet`, a template for new projects.

```
 Phones / laptops (installable web app; records with no signal, uploads later)
          │ HTTPS
          ▼
 Server:  Caddy ──► app.py ──► SQLite        (nightly copy, kept 30 days)
                      ├─► Whisper large-v3   (NVIDIA GPU if there is one, else CPU)
                      ├─► Ollama ─► Gemma 4 12B
                      ├─► Slack          (optional; the server connects out, no open port)
                      └─► Google Sheet   (optional; synced about once a minute)
```

# Setting it up

This part is for the person putting the app on a server. Do the steps in order; each one says how to check it worked. Expect about an hour, plus waiting while about 11 GB of AI models download.

**Who does what**

| Who | What | When |
|---|---|---|
| Admin (you) | Prepare the server, install, create logins, connect Slack and the sheet | Steps 1–8 |
| A Slack workspace owner or admin | Create and install the Slack app (can be you, if you are one) | Step 6, optional |
| A Google Workspace super admin | Allow one service-account key for one Cloud project | Step 7, optional |

**What you need**

- An **Ubuntu 22.04 or 24.04** server you can reach over SSH as a user with `sudo`.
- Ideally an **NVIDIA GPU with 20 GB or more** of memory (for example an RTX 4090, L4 or A10): notes become text in seconds. Without one it still works, in minutes per note; then give it at least 24 GB of RAM (32 GB is comfortable).
- At least **40 GB of free disk** (the models alone are about 11 GB).
- Optional: a domain or subdomain you can point at the server, such as `timesheet.yourcompany.com`. Without one the app gets a free address based on the server's IP.

## 1. Prepare the server

1. If your provider has its own firewall (Hostinger has one in **hPanel**), allow ports **22, 80 and 443**.
2. Turn on your provider's **automatic server backups**. The app's own nightly backups stay on the same server, so they don't help if the server is lost.
3. If you're using your own domain, create a DNS **A record** pointing it at the server's IP now, so it has spread by step 3.
4. SSH into the server.

## 2. Get the code onto the server

```bash
sudo git clone https://github.com/itej13/Trackr.git /opt/timesheet
```

Keep the code in `/opt/timesheet`: the app's service can't read home folders.

**Check:** `ls /opt/timesheet` shows `app.py`, `deploy/`, `static/`.

## 3. Install

```bash
sudo /opt/timesheet/deploy/setup.sh                            # free address: <server-ip>.sslip.io
sudo /opt/timesheet/deploy/setup.sh timesheet.yourcompany.com  # or your own domain (its DNS must point here)
```

It installs the system packages, Python packages, Ollama, Caddy (for HTTPS) and the GPU libraries; downloads both models; sets up the app as a service that restarts by itself, a nightly backup and a firewall that lets in only SSH and web traffic; then runs both models once as a self-test. It's safe to run again: that's also how you update.

**A server that already hosts a website** (something else answers on ports 80/443): the script sees it and leaves that web server and the firewall alone, without installing Caddy. Afterwards, set that web server to forward the timesheet's address to `http://127.0.0.1:8000`, allowing uploads up to 60 MB and passing `X-Forwarded-For`. The AI runs at low priority, so it uses the processor only when the website doesn't need it.

- If it says **"NVIDIA GPU found but its driver isn't installed … Reboot"**, run `sudo reboot`, SSH back in and run the same command again.
- At the end it prints the address, for example `https://203-0-113-7.sslip.io`. The HTTPS certificate is fetched on the first visit, so allow a minute.
- If it ends with **"The self-test failed"**, the site works but notes won't be transcribed. See Troubleshooting.

**Check:** open the address. You see the login page with a padlock in the address bar.

## 4. Create your admin login

```bash
sudo /opt/timesheet/deploy/ts adduser yourlogin "Your Name" --admin --role Founder --email you@yourcompany.com
```

It asks for a password twice (at least 10 characters). Log in at the address. You see the **Today** screen with a record button, and the admin pages: Team, Tasks, Reports, Company and Settings.

## 5. Add the team and teach it your words

1. **Team → Add a person** for each employee: login ID, name, role, **work email** (the one they use in Slack) and a first password of at least 10 characters. Give each person their login privately. People can also be added from the Google Sheet later (step 7).
2. **Settings → Glossary:** add your client and brand names and the tools you use. Speech recognition uses this list to spell them right. Keep it under about 100 terms.
3. **Settings → Speaking guide:** the short "how to record your update" text on top of everyone's Today screen. The default works; change it if you like.
4. Tell people to open the address on their phone and choose **Add to Home Screen** (iPhone, from Safari's Share menu) or **Install app** (Android Chrome). Recording then works even with no signal: the note waits on the phone and uploads by itself.

## 6. Connect Slack (optional)

People can then send their update as a voice note in a direct message to the **Timesheet** bot. The bot answers in the thread with what it heard, the summary and a **Submit** button. No public address or open port is needed: the server connects out to Slack.

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
   The bot can read only messages sent to it directly, the files in them, and people's names and emails (to match them to their logins). It can't read channels.
2. **Basic Information → App-Level Tokens → Generate Token and Scopes:** any name, scope `connections:write`. Copy the token (starts with `xapp-`).
3. **Install App → Install to Workspace → Allow.** Then **OAuth & Permissions:** copy the **Bot User OAuth Token** (starts with `xoxb-`).
4. Put both tokens on the server, readable only by the app:
   ```bash
   sudo install -o root -g timesheet -m 640 /dev/null /var/lib/timesheet/slack.env
   sudo nano /var/lib/timesheet/slack.env
   ```
   Write these two lines with your tokens, no quotes or spaces, then save (Ctrl+O, Enter, Ctrl+X):
   ```
   SLACK_APP_TOKEN=xapp-…
   SLACK_BOT_TOKEN=xoxb-…
   ```
   ```bash
   sudo systemctl restart timesheet
   ```

**Check:** **Settings → Slack** says *Connected*. Send the bot "hi" in Slack (find it under **Apps**): it replies with the speaking guide. Then send it a short voice clip: the bot replies "Got it", and that reply then changes to show the text and a Submit button (seconds on a GPU, minutes without).

**Matching people:** a Slack account is matched to a login by **work email**. If nobody matches, the note waits under **Team → Slack notes waiting for a person**; pick who it is and click **Link**. Those notes aren't downloaded until then.

## 7. Connect a Google Sheet (optional)

A Google Sheet then becomes a second control panel: **People** and **Tasks** sync both ways about once a minute (edit names, roles, Admin/Active, work emails, task fields, or add rows); **Entries** and **Day summaries** are copied there to read. The app keeps working if Google is down.

1. Create an empty Google Sheet in the company Google account. **Share it only with admins**: it will hold everyone's transcripts.
2. In https://console.cloud.google.com create a project (for example `timesheet-sync`), then **APIs & Services → Library → Google Sheets API → Enable**.
3. **IAM & Admin → Service accounts → Create service account** (for example `timesheet-sync`). It needs no roles; skip that step.
4. Open the new service account: **Keys → Add key → Create new key → JSON**. A file downloads.
   If Google refuses, your Workspace blocks service-account keys (organisations created on or after 3 May 2024 do by default). A super admin can allow it for this project only: with the project selected, open **IAM & Admin → Organization policies**, find **Disable service account key creation**, and override it for this project so it is no longer enforced. That needs the *Organization Policy Administrator* role, which a super admin can give themselves.
5. In the sheet, **Share** it with the service account's email (it ends in `iam.gserviceaccount.com`; the app also shows it) as **Editor**, with "Notify" unticked.
6. Copy the key file to the server (for example with `scp`), install it so only the app can read it, then delete every other copy:
   ```bash
   sudo install -o root -g timesheet -m 640 key.json /var/lib/timesheet/google-key.json && rm key.json
   ```
7. In the app: **Settings → Google Sheet**, paste the sheet's link from the browser's address bar, **Save**.

**Check:** within a minute the sheet has four tabs (People, Tasks, Entries, Day summaries) and **Settings → Google Sheet** shows "Last sync" with no red error. **Sync now** runs it at once.

How the sheet behaves:
- **Passwords:** type one in **New password**; the app sets it at the next sync, clears the cell and writes "Password set" in **Sync note**. Google's version history still keeps what was typed, which is one more reason to share the sheet with admins only.
- **Deleting a row** deactivates that person or cancels that open task. More than 5 rows gone from a tab at once is treated as an accident, and they're put back.
- **A row the app can't use** (unknown login ID, bad date, password under 10 characters, …) is skipped as a whole, with the reason in **Sync note**. If the app and the sheet changed the same cell between syncs, the app's value wins and Sync note says so.
- Don't edit the first row (the headers): a tab whose headers changed isn't synced until they're put back.

## 8. Check everything works

| Check | Command or place | Good result |
|---|---|---|
| Both AI models run | `sudo /opt/timesheet/deploy/ts selftest` | Whisper and Summaries both say `ok` (no `FAILED`) |
| App is up | `curl -s https://<address>/api/health` | `"ok": true` and `"ollama": true` |
| Recording works | Record a 10-second note on your phone | Within a minute: the text, then a summary |
| Backups are scheduled | `systemctl list-timers timesheet-backup.timer` | A next run is listed (02:30 India time, shown in the server's own clock, e.g. 21:00 UTC) |
| Slack (if set up) | Settings → Slack | *Connected* |
| Sheet (if set up) | Settings → Google Sheet | "Last sync" with no error |

Then pick 3–5 people for a week's pilot before inviting everyone.

# Running it

| What | Command |
|---|---|
| Update to the latest code | `cd /opt/timesheet && sudo git pull && sudo deploy/setup.sh` |
| Add a person (or use **Team** in the app) | `sudo /opt/timesheet/deploy/ts adduser riya "Riya Sharma" --role "Video editor" --email riya@yourcompany.com` |
| Check both models work | `sudo /opt/timesheet/deploy/ts selftest` |
| Live log | `journalctl -u timesheet -f` |
| Restart the app | `sudo systemctl restart timesheet` |
| Uptime monitor URL | `https://<address>/api/health` |
| Backups | `/var/lib/timesheet/backups/`: one per night (newest 30 kept), plus a copy made before each database upgrade |

**Restore a backup** (this replaces the current data with that night's copy):

```bash
sudo systemctl stop timesheet
sudo install -o timesheet -g timesheet -m 640 /var/lib/timesheet/backups/timesheet-2026-11-20.db /var/lib/timesheet/timesheet.db
sudo rm -f /var/lib/timesheet/timesheet.db-wal /var/lib/timesheet/timesheet.db-shm
sudo systemctl start timesheet
```

**Where things live**

| Path | What |
|---|---|
| `/opt/timesheet` | The code (this repo) |
| `/var/lib/timesheet/timesheet.db` | All the data |
| `/var/lib/timesheet/backups/` | Database copies |
| `/var/lib/timesheet/models/` | The Whisper model (Ollama keeps Gemma in its own folder) |
| `/var/lib/timesheet/slack.env`, `google-key.json` | Slack tokens and the Google key: `root:timesheet`, mode 640 |
| `/etc/timesheet.env` | Settings written by `setup.sh`; it rewrites this file on every run, so don't edit it by hand |
| Services | `timesheet` (the app), `timesheet-backup.timer`, `caddy` (HTTPS), `ollama` |

**Secrets:** the Slack tokens and the Google key file never go in this repo, an email or a chat message. If one leaks, replace it: for Slack, make new tokens in the app's settings at api.slack.com and put them in `slack.env`; for Google, delete the key under the service account's **Keys**, make a new one and install it as in step 7.

## Troubleshooting

| Problem | What to check |
|---|---|
| The address doesn't open, or a certificate warning | Ports 80 and 443 open in the provider's firewall; the domain's A record points at this server; `journalctl -u caddy -n 50` |
| "The microphone only works over HTTPS" or no microphone | Use the `https://` address; allow the microphone for the site in the browser's settings |
| Notes stay on "Transcribing…" or the self-test failed | `sudo /opt/timesheet/deploy/ts selftest`, then `journalctl -u timesheet -n 100`; `systemctl status ollama` and `ollama list` should show `gemma4:12b` |
| Very slow (minutes per note) on a GPU server | `nvidia-smi` should list the card. If not, install the driver (`setup.sh` does it), reboot, run `setup.sh` again |
| "Too many attempts" at login | Wait 15 minutes, or restart the app (`sudo systemctl restart timesheet`) |
| Forgot the admin password | Another admin sets a new one under **Team → Edit**, or create a new admin with `ts adduser … --admin` |
| Settings → Slack: *Not connected* | `journalctl -u timesheet | grep Slack` shows why. Usually a token with spaces or quotes in `slack.env`, or the app isn't installed. Restart after fixing |
| The Slack bot doesn't answer | The app's **App Home → Messages Tab** must be on and `message.im` subscribed (the manifest does both). After changing scopes, reinstall the app |
| Someone's Slack notes wait under Team | Their work email in the app doesn't match their Slack email. Fix it, or **Link** them there |
| Sheet: "Google said 403" | Share the sheet with the service account's email as Editor |
| Sheet: "Google said 404" | The saved link isn't that sheet; paste it again from the address bar |
| Sheet: "invalid_grant" | The server clock is off (`timedatectl` should say synchronized), or the key was deleted in Google: make a new one |
| Sheet: "…wasn't synced: its first row must be…" | Someone edited a header row; put it back as shown |

# How it behaves

- **Editing:** today's entries can be edited, re-recorded or deleted until midnight India time. Older days are read-only. A draft left unsubmitted at midnight, or a recording made yesterday that uploads today, is submitted automatically, so nothing said is lost.
- **Edits are visible:** when someone edits an already-submitted entry, admins see "edited" and the text from before the edit.
- **Audio is deleted** as soon as it's been transcribed. Only text is stored. (Voice notes sent through Slack also stay in Slack.)
- **Comments** are visible to admins only.
- **Summaries:** day summaries are written automatically. Monthly, company and date-range summaries are written when an admin clicks Generate or Refresh.
- **Tasks:** admins give tasks to people (Tasks page); each person sees theirs under **My tasks** and taps Start or Done. After a voice note, the AI checks it against that person's open tasks and may ask "Looks like you finished X. Mark it done?". Nothing changes until they tap Mark done.
- **Reports** (admin): any date range up to 92 days, one person or everyone: who logged which days, each day's summary and exact words, and a summary of the range on request.
- **Security:** the app and Ollama listen only inside the server, and the firewall lets in only SSH and web traffic. Passwords need at least 10 characters. Logins lock for 15 minutes after 5 wrong passwords for one login ID, or 50 from one address. Pages are served with strict browser security headers.

# Configuration

`deploy/setup.sh` writes these to `/etc/timesheet.env`.

| Variable | Default | |
|---|---|---|
| `TS_HOST`, `TS_PORT` | `127.0.0.1`, `8000` | Where the app listens; Caddy forwards to it |
| `TS_PUBLIC_URL` | unset | The public `https://` address. Turns on secure cookies, reads the visitor's IP from Caddy, and adds "Edit in app" links to Slack replies |
| `TS_DB` | `data/timesheet.db` | On the server: `/var/lib/timesheet/timesheet.db`. The Slack and Google files are read from the same folder |
| `TS_WHISPER` | `large-v3` | Don't use `large-v3-turbo`: it can't translate Hindi to English |
| `TS_WHISPER_DEVICE` | NVIDIA GPU if found, Apple GPU on a Mac, else `cpu` | |
| `TS_WHISPER_COMPUTE` | `float16` on a GPU, `int8` on CPU | |
| `TS_LLM` | `gemma4:12b` | Any Ollama model. Tested against `qwen2.5:14b`, `qwen3:8b` and `qwen3.5:9b` on the demo data: Gemma was the only one that invented nothing and got every task check right, and it's about twice as fast as Qwen 2.5 14B |
| `TS_OLLAMA` | `http://127.0.0.1:11434` | |

# Development

Run it on your own computer:

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
ollama pull gemma4:12b
.venv/bin/python -c "from faster_whisper import download_model; download_model('large-v3')"
.venv/bin/python app.py adduser admin "Your Name" --admin
.venv/bin/python app.py                      # http://localhost:8000
```

Locally, Slack tokens go in `data/slack.env` and the Google key in `data/google-key.json`; `data/` is never committed.

Check the rules with the models faked:

```bash
.venv/bin/python test_app.py
```

This covers access rules, drafts, the midnight lock, late uploads, edits, summaries, reports, tasks and the "task done?" check, the Google Sheet sync and the Slack bot (against stand-ins for Google and Slack), database upgrades, the login throttle and security headers.

# Known limits

- **Day and per-person monthly summaries are reliable** in tests: right clients and tools, and work redone later isn't reported as still open.
- **The company summary is an overview, not a ledger.** In a 50-person test it kept every client and every one-off problem, but per-client counts were made up and one blocker was put under the wrong client. Check numbers in each person's monthly summary.
- The company summary is one background job that first writes any missing monthly summaries. For 40–50 people on a GPU expect about 10 minutes, and day summaries wait while it runs.
- **Without a GPU** everything still works, but transcripts and summaries take minutes instead of seconds, and an evening rush builds a queue. Nobody has to wait for it: a note still in the queue at midnight is kept as submitted. Monthly and company summaries are written between 1 and 2 am, so they're ready in the morning.
- **Backups stay on the same server** unless the provider's server backups are on (step 1).
- No password reset by email. An admin sets a new password under **Team → Edit**.
