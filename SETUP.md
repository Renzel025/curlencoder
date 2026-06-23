# Baccarat Encoder Monitor + Lark Bot — setup & architecture

Two components in this repo, sharing **one Lark app**, **one server** (`/opt/curlencoder`),
and **one state file** (`last_run.json`):

| Component | File | Runs as | Job |
|---|---|---|---|
| **Monitor** | `encoder_monitor.py` | **cron** (Tue/Thu 06:59) | curls encoders, fills the Lark sheets, posts a summary card, writes `last_run.json` |
| **Bot** | `lark_ai_bot.py` | **systemd** service (always on) | answers commands in the OTE Lark group (reads `last_run.json`, curls encoders on demand) |

The monitor is **stdlib-only Python 3.6** (system `python3`). The bot needs **Python 3.8+**
(`lark-oapi`), so it runs in a venv at `/opt/curlencoder/botenv`.

---

## Architecture

```
                          ┌──────────────────────────────────────┐
                          │  Lark  (casinoplus.sg.larksuite.com)  │
                          │  • OTE group chat                      │
                          │  • per-studio 4-tab spreadsheets       │
                          │    Encoder (PC) / (SDK) / TRTC / Agora │
                          └───────▲───────────────────┬───────────┘
              posts card,         │                   │  @mention commands
              fills PC/SDK tabs   │                   │  (over long-connection)
                          ┌───────┴────────┐   ┌──────▼───────────┐
                          │ encoder_monitor│   │   lark_ai_bot    │
                          │ .py   (cron)   │   │   .py (systemd)  │
                          │ Tue/Thu 06:59  │   │  WebSocket + Groq│
                          └───────┬────────┘   └──────┬───────────┘
                       writes     │                   │ reads
                                  ▼                   ▼
                          ┌──────────────────────────────────────┐
                          │   /opt/curlencoder/last_run.json      │
                          │   (every encoder: studio/tab/ip/status)│
                          └──────────────────────────────────────┘
                                  │ both curl encoders (HTTP digest)
                                  ▼
                          ┌──────────────────────────────────────┐
                          │  Encoders  http://<ip>/get_output...  │
                          └──────────────────────────────────────┘
```

---

## How the MONITOR works (cron)

```
cron → run_monitor.sh → (source encoder_monitor.env) → encoder_monitor.py
  │
  ├─ for each studio in LARK_STUDIOS  (name|token|pc_tab|sdk_tab|trtc_tab|agora_tab):
  │     ├─ Encoder (PC) tab:  read blocks → curl each IP /get_output?input=0&output=0/1/2
  │     │      → parse RoomID/UserID/SDKAppID/Usersig/PrivateMapKey + Agora URL
  │     │      → write values into the block's E/F/G rows (Mainstream/Sub1/Sub2)
  │     ├─ Encoder (SDK) tab:  same
  │     └─ TRTC + Agora flat tabs:  SKIPPED (default).  Set FLAT_TABS=1 to fill them.
  │
  ├─ post a summary card to the OTE group:
  │     "LAVIE Sheet records  ENCODER (PC) 18/19 · ENCODER (SDK)-NEW 20/21"
  │     🔴 Unreachable / ⚠️ Template missing param labels
  │
  └─ write last_run.json  (every encoder with status: ok | unreachable | no_trtc | no_labels)
```

Key rule: the script writes each value into the row **labeled** in column D
(`RoomID`/`UserID`/...). A tab with no labels in column D = nothing to write
(only the Agora row fills) → flagged "template missing param labels".

## How the BOT works (systemd)

```
user @mentions bot → Lark pushes im.message.receive_v1 over the long connection
  │
  └─ on_message:
       ├─ skip if duplicate / older than 120s (replay guard)
       ├─ 👍 react, parse the first word as a command:
       │     update                       → recorded/not-recorded per studio & tab (from last_run.json)
       │     curl                         → re-curl every unreachable encoder, report
       │     pc | sdk | trtc | agora      → recorded/not-recorded list for that tab
       │     pc <table> | sdk <table>     → curl encoder LIVE → full block card
       │     trtc <room> | agora <table>  → curl LIVE → that one URL
       │     usersig/userid/... <table>   → curl LIVE → that one field (all 3 streams)
       │     (anything else)              → help card
       └─ ✅ react when the reply is posted
```

Commands are **deterministic** (no LLM) for reliability. Free-form chat is off by
default (`CHAT_MODE=commands`); set `CHAT_MODE=llm` to allow the Groq model to answer.

---

## Setup

### 1. Get the repo onto the server

Repo: **`github.com/Renzel025/curlencoder`**, deployed at **`/opt/curlencoder`**.

**First time — clone:**
```bash
sudo yum install -y git python38                 # prereqs (or apt: git python3.8)
cd /opt
sudo git clone https://github.com/Renzel025/curlencoder.git
cd /opt/curlencoder
```
If the repo is private, clone with a GitHub Personal Access Token (then `git pull`
reuses it):
```bash
sudo git clone https://<YOUR_TOKEN>@github.com/Renzel025/curlencoder.git /opt/curlencoder
```

**Later — update:**
```bash
cd /opt/curlencoder && git pull
sudo systemctl restart lark-ai-bot               # bot must reload; cron picks up new code itself
```

**Create the secret env files** (one time, from the templates — they're gitignored,
so `git pull` never touches them):
```bash
cd /opt/curlencoder
cp encoder_monitor.env.example encoder_monitor.env && chmod 600 encoder_monitor.env
cp lark_ai_bot.env.example     lark_ai_bot.env     && chmod 600 lark_ai_bot.env
```

### 2. Lark Custom App (one app, used by BOTH)
In the Lark Developer Console:
- Copy **App ID** (`cli_...`) + **App Secret** → into both env files (same app).
- **Scopes:** `im:message`, `im:message:send_as_bot`, `im:message.reaction:write`,
  `im:message.group_at_msg:readonly`, `im:message.p2p_msg:readonly`, `sheets:spreadsheet`.
- **Events & Callbacks:** mode = **"Receive events through persistent connection"**
  (no webhook). Add event **`im.message.receive_v1`**.
- **Enable the Bot feature**, add the bot to the **OTE group**, share each studio
  **spreadsheet with edit access**.
- **Release a version** (nothing takes effect until released).

### 3. Monitor config — `encoder_monitor.env`
```bash
export LARK_APP_ID="cli_..."
export LARK_APP_SECRET="..."
export LARK_CHAT_ID="oc_..."                # OTE group
export ENCODER_USER="admin"; export ENCODER_PASS="admin"
# One line per studio:  name|token|pc_tab|sdk_tab|trtc_tab|agora_tab
export LARK_STUDIOS="
lavie|GYvOsizYJhEmPKtPuEwlrAWOgDo|0kuxDh|1HRTLV|2UpMgU|3mwXyn
stots|MSqKsnuXzh704Kt2BIMlg7vlgec|0QdHKA|1sIyLi|2YvCVp|3ufzDi
dheights|V03RsV3THhlAfjtq2RNlSRIpgAd|0PWPHX|1hgoah|2fDTpu|3WMRkN
newport|BH6NsmhkOhwWB4tBQEdlK1IwgZf|0dPOFO|1Lpzfl|2MakKG|3DwzKp
"
# TRTC_MODE=construct (build URL from creds) ; FLAT_TABS=0 (skip TRTC/Agora tabs)
```
Get each tab id from its URL (`...?sheet=XXXX`), or run:
`python3 encoder_monitor.py list-tabs <token>`.

### 4. Test the monitor + schedule the cron

**a) Manual test first** (does exactly what cron will do):
```bash
chmod +x run_monitor.sh
./run_monitor.sh                          # card posts + PC/SDK sheets fill
```

**b) Add the cron — Tue + Thu 06:59** (one-liner, no editor; keeps existing jobs):
```bash
(crontab -l 2>/dev/null; echo "59 6 * * 2,4 /opt/curlencoder/run_monitor.sh >> /opt/curlencoder/monitor.log 2>&1") | crontab -
```

**c) Confirm + watch it fire:**
```bash
crontab -l                                # should list the 59 6 * * 2,4 line
tail -f /opt/curlencoder/monitor.log      # log is created on the FIRST cron run
```

Notes:
- Always schedule the **wrapper** `run_monitor.sh`, never the `.py` directly — cron
  has no environment, and the wrapper sources `encoder_monitor.env` first.
- `59 6 * * 2,4` = 06:59 every **Tue & Thu** (`min hr * * dow`; 2=Tue, 4=Thu). Server
  is **CST (UTC+8 = PH time)** — confirm with `date`.
- Other schedules: weekly Mon 07:00 = `0 7 * * 1`; daily 5:30 PM = `30 17 * * *`.
- To edit by hand instead: `export EDITOR=nano; crontab -e`.
- To remove the job: `crontab -l | grep -v run_monitor.sh | crontab -`.

### 5. Bot — venv, env, service
```bash
python3.8 -m venv /opt/curlencoder/botenv          # one time (needs Python 3.8+)
/opt/curlencoder/botenv/bin/pip install -r requirements.txt
nano lark_ai_bot.env       # LARK_APP_ID/SECRET (same app), OPENAI_* (Groq), ENCODER_*

sudo cp lark-ai-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lark-ai-bot
journalctl -u lark-ai-bot -f                        # watch logs
```
### 6. Optional — free-form chat via Groq (LLM)

The bot is **command-only by default** (`CHAT_MODE=commands`) and needs **NO LLM key** —
all the commands (`update`, `curl`, `pc`, `sdk`, field lookups) are deterministic.
Enable an LLM only if you want it to answer free-form questions too.

We use **Groq** (free, OpenAI-compatible API):

1. Get a key at <https://console.groq.com/keys> (starts `gsk_...`).
2. In `lark_ai_bot.env`:
   ```bash
   export CHAT_MODE="llm"
   export OPENAI_API_KEY="gsk_...your_groq_key..."
   export OPENAI_BASE="https://api.groq.com/openai/v1"
   export OPENAI_MODEL="llama-3.3-70b-versatile"   # or llama-3.1-8b-instant (higher rate limits)
   ```
3. `sudo systemctl restart lark-ai-bot`

Notes:
- The vars are named `OPENAI_*` but point at **Groq**. Any OpenAI-compatible API works
  — for real OpenAI: `OPENAI_BASE=https://api.openai.com/v1`, key `sk-...`, model `gpt-4o-mini`.
- **Commands stay deterministic regardless of `CHAT_MODE`** — the LLM only handles
  messages that aren't a known command.
- Groq's free tier rate-limits; if you see "couldn't reach the AI service", switch to
  `llama-3.1-8b-instant` or send fewer messages.
- Groq sits behind Cloudflare — the bot already sends a User-Agent header to avoid the
  `403 error code: 1010` block.

### Deploy rule (this bites everyone)
> **`git pull` updates files; the running bot only loads them on restart.**
> After pulling bot changes: `sudo systemctl restart lark-ai-bot`.
> The monitor (cron) picks up new code automatically on its next run.

---

## Bot commands (in the OTE group, @mention the bot)

| Command | What it does |
|---|---|
| `update` | recorded ✅ / not-recorded ❌ per studio & tab (from last run) |
| `curl` | re-curl every encoder that was unreachable, report now-reachable vs still-down |
| `pc` / `sdk` | list recorded / not-recorded encoders on that tab |
| `trtc` / `agora` | list rooms that got a URL vs missing (only if FLAT_TABS was on) |
| `pc ENP01_PC` / `sdk ENP01` | full block: Agora + SDK TRTC (RoomID/UserID/SDKAppID/PrivateMapKey/Usersig) across all 3 streams |
| `usersig ELV01_PC` | one field for all 3 streams (also `userid`/`sdkappid`/`privatemapkey`/`roomid`) |
| `trtc ENP01_MAIN` / `agora ENP01_PC` | the single TRTC / Agora URL |

Commands are case-insensitive. `pc`/`sdk`/`field` lookups curl the encoder **live**
(current values); listings read the last monitor run.

---

## Files
| File | Purpose |
|---|---|
| `encoder_monitor.py` | monitor (cron) — stdlib only, Python 3.6+ |
| `run_monitor.sh` | cron wrapper (sources env, runs the monitor) |
| `lark_ai_bot.py` | Lark bot (systemd) — needs `lark-oapi` (venv, Python 3.8+) |
| `lark-ai-bot.service` | systemd unit for the bot |
| `requirements.txt` | `lark-oapi` (bot only) |
| `*.env` | secrets — gitignored; `*.env.example` are the templates |
| `last_run.json` | shared state the monitor writes and the bot reads (gitignored) |

## Notes / gotchas
- Server timezone is **CST (UTC+8 = PH time)** — confirm with `date`.
- A bot `.env` value (e.g. `SYSTEM_PROMPT`) **overrides** the code default — to use
  the code default, remove the line from the env.
- TRTC URL is **constructed** from creds (`TRTC_MODE=construct`) — encoders on the
  native TRTC SDK expose no rtmp URL to scrape.
- All outputs share a RoomID but have different UserIDs; the sheet/bot use the
  **Mainstream (output 0)** values for the room.
