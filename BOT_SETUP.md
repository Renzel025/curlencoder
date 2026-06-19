# Lark AI Bot — setup & operations

An AI chatbot for Lark: DM it or @mention it in a group, and it replies in-thread
with an answer from an LLM (Groq or OpenAI). Code: [`lark_ai_bot.py`](lark_ai_bot.py).

It uses Lark's **long-connection (WebSocket)** event mode — **no webhook / no public
URL needed.** Reuses the SAME Lark Custom App as `encoder_monitor.py`.

---

## 1. One-time install on the server

The bot needs **Python 3.8+** (the system `python3` is 3.6 — too old for the Lark
SDK, so we use a dedicated venv and leave the system Python alone for the cron job).

```bash
cd /opt/curlencoder
git pull

# create the bot's venv (only once)
python3.8 -m venv /opt/curlencoder/botenv
/opt/curlencoder/botenv/bin/pip install -r requirements.txt

# secrets (gitignored — never committed)
cp lark_ai_bot.env.example lark_ai_bot.env
chmod 600 lark_ai_bot.env
nano lark_ai_bot.env      # fill in the values below
```

### What to put in `lark_ai_bot.env`
| Var | Value |
|-----|-------|
| `LARK_APP_ID` / `LARK_APP_SECRET` | same as `encoder_monitor.env` |
| `LARK_DOMAIN` | `https://open.larksuite.com` (Lark intl) — default, can omit |
| `OPENAI_API_KEY` | the LLM key — a Groq `gsk_...` or OpenAI `sk-...` key |
| `OPENAI_BASE` | **Groq:** `https://api.groq.com/openai/v1` · **OpenAI:** `https://api.openai.com/v1` |
| `OPENAI_MODEL` | **Groq:** `llama-3.3-70b-versatile` · **OpenAI:** `gpt-4o-mini` |
| `SYSTEM_PROMPT` | the bot's persona / instructions |

> The vars are named `OPENAI_*` but work for any OpenAI-compatible API (Groq included).

---

## 2. One-time setup in the Lark Developer Console

At <https://open.larksuite.com> → your app:

1. **Events & Callbacks → Events:** subscription mode = **"Receive events through
   persistent connection"** (no URL). Save, then **Add events →** `im.message.receive_v1`.
2. **Permissions & Scopes — add:**
   - `im:message.group_at_msg:readonly` (hear @mentions in groups)
   - `im:message.p2p_msg:readonly` (hear DMs)
   - `im:message:send_as_bot` (reply)
3. **Version Management & Release → create & release a new version.** Nothing goes
   live until released.
4. Add the bot to the group(s) you want it in.

---

## 3. Run it as a service (24/7)

```bash
sudo cp /opt/curlencoder/lark-ai-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now lark-ai-bot
```

### Operate
```bash
sudo systemctl status lark-ai-bot     # is it running?
journalctl -u lark-ai-bot -f          # live logs
sudo systemctl restart lark-ai-bot    # after editing lark_ai_bot.env or git pull
sudo systemctl stop lark-ai-bot       # stop it
```

After a `git pull` (new code) or editing `lark_ai_bot.env` (new key/model),
**restart** the service for changes to take effect.

---

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `Incorrect domain name` on connect | `LARK_DOMAIN` must match the app's cluster (larksuite vs feishu). |
| Connects fine but **no log when you @mention** | Event `im.message.receive_v1` not subscribed, scopes missing, or version not released. |
| `LLM API HTTP 401 invalid_api_key` | Wrong/placeholder `OPENAI_API_KEY`. |
| `LLM API HTTP 403: error code: 1010` | Cloudflare blocking the client signature — fixed by the User-Agent header in the code. If it persists, the server's IP/region is blocked; use OpenAI or a proxy. |
| `LLM API HTTP 404 model_not_found` | `OPENAI_MODEL` isn't an exact model id. |
| Hears you but never replies | Missing `im:message:send_as_bot` scope. |
