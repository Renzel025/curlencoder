# Lark Bot — setup & operations

A **command bot** for the OTE Lark group: @mention it with a command and it replies
with a card. Code: [`lark_ai_bot.py`](lark_ai_bot.py).

By default it is **command-only** (no LLM) — unknown messages get a help card. Set
`CHAT_MODE=llm` to also allow free-form chat via Groq. It uses Lark's
**long-connection (WebSocket)** mode — **no webhook needed** — and the **same Lark
app** as `encoder_monitor.py`. It reads the monitor's `last_run.json` and curls
encoders live to answer.

---

## Commands (@mention the bot; case-insensitive)

| Command | What it does |
|---|---|
| `update` | recorded ✅ / not-recorded ❌ per studio & tab (from the last monitor run) |
| `curl` | re-curl every encoder that was unreachable; reports now-reachable vs still-down |
| `pc` / `sdk` | list recorded / not-recorded encoders on that tab |
| `trtc` / `agora` | list rooms that got a URL vs missing (only if the monitor ran with `FLAT_TABS=1`) |
| `pc ENP01_PC` / `sdk ENP01` | **full block** — Agora + SDK TRTC (RoomID/UserID/SDKAppID/PrivateMapKey/Usersig) across Mainstream / Substream 1 / Substream 2 |
| `usersig ELV01_PC` | one field across all 3 streams (also `userid`, `sdkappid`, `privatemapkey`, `roomid`) |
| `trtc ENP01_MAIN` / `agora ENP01_PC` | the single TRTC / Agora URL |

- **Listings** (`update`, `pc`, `sdk`, `trtc`, `agora`) read `last_run.json` (last monitor run).
- **Lookups** (`pc/sdk <table>`, field commands) curl the encoder **live** → current values.
- Tab-scoped matching: `sdk ENP01` finds the SDK `ENP01` (not `ENP01_PC`).
- All commands are deterministic (no LLM) → no hallucinated IPs or leaked tool syntax.

---

## 1. One-time install on the server

The bot needs **Python 3.8+** (system `python3` is 3.6 — too old for `lark-oapi`),
so it runs in a dedicated venv. The cron monitor keeps using system Python.

```bash
cd /opt/curlencoder
git pull
python3.8 -m venv /opt/curlencoder/botenv          # one time
/opt/curlencoder/botenv/bin/pip install -r requirements.txt
cp lark_ai_bot.env.example lark_ai_bot.env && chmod 600 lark_ai_bot.env
nano lark_ai_bot.env
```

### `lark_ai_bot.env`
| Var | Value |
|-----|-------|
| `LARK_APP_ID` / `LARK_APP_SECRET` | same app as `encoder_monitor.env` |
| `LARK_DOMAIN` | `https://open.larksuite.com` (default; Feishu = `open.feishu.cn`) |
| `ENCODER_USER` / `ENCODER_PASS` / `ENCODER_SCHEME` / `ENCODER_INPUT` | same as `encoder_monitor.env` — needed to curl encoders for the commands |
| `STATE_FILE` | path to `last_run.json` — defaults to `<repo>/last_run.json` (matches the monitor) |
| `CHAT_MODE` | `commands` (default, command-only) or `llm` (also free-form chat) |
| `REACTIONS` / `REACT_ACK` / `REACT_DONE` | `1` / `THUMBSUP` / `DONE` — the 👍 on-receive, ✅ on-done reactions |
| `OPENAI_API_KEY` / `OPENAI_BASE` / `OPENAI_MODEL` | **only used when `CHAT_MODE=llm`.** Groq: `gsk_...` / `https://api.groq.com/openai/v1` / `llama-3.3-70b-versatile` |
| `SYSTEM_PROMPT` | only used when `CHAT_MODE=llm`. **An env value here overrides the code default** — remove the line to use the code default. |

---

## 2. Lark Developer Console (one time)

At <https://open.larksuite.com> → your app:
1. **Events & Callbacks → Events:** mode = **"Receive events through persistent
   connection"** (no URL). **Add event** `im.message.receive_v1`.
2. **Permissions & Scopes** — add:
   - `im:message.group_at_msg:readonly` (hear @mentions in groups)
   - `im:message.p2p_msg:readonly` (hear DMs)
   - `im:message:send_as_bot` (reply)
   - `im:message.reaction:write` (the 👍 / ✅ reactions)
3. **Enable the Bot feature**, add the bot to the **OTE group**.
4. **Release a version** — nothing takes effect until released.

---

## 3. Run as a service (24/7)

```bash
sudo cp /opt/curlencoder/lark-ai-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lark-ai-bot
```

### Operate
```bash
sudo systemctl status lark-ai-bot     # running?
journalctl -u lark-ai-bot -f          # live logs
sudo systemctl restart lark-ai-bot    # after git pull or editing lark_ai_bot.env
```

> ⚠️ **`git pull` updates files; the running bot only loads them on RESTART.**
> Always `sudo systemctl restart lark-ai-bot` after pulling bot changes — and kill
> any stray manual `python lark_ai_bot.py` (`pkill -f lark_ai_bot.py`) so only the
> systemd process answers.

---

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| Commands do nothing / old behavior | Bot not restarted after `git pull`, or a stray manual process is answering. `pkill -f lark_ai_bot.py` then restart. |
| `Incorrect domain name` on connect | `LARK_DOMAIN` must match the app's cluster (larksuite vs feishu). |
| Connects but **no log when you @mention** | Event `im.message.receive_v1` not subscribed, scope missing, or version not released. |
| A reaction doesn't show (e.g. 👍) | That emoji key isn't valid on your tenant — try `THUMBSUP` / `OK` / `DONE`. |
| `update` / `trtc` listing empty | Monitor hasn't written `last_run.json` with the new format yet — run the monitor once. |
| `LLM API HTTP 401 / 403 1010 / 404` | (CHAT_MODE=llm only) bad key / Cloudflare block / wrong model id. |
