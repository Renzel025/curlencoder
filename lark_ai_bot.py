#!/usr/bin/env python3
"""
lark_ai_bot.py
==============
An AI chatbot for Lark — message it (DM) or @mention it in a group, and it
replies in-thread with an answer from OpenAI (GPT). Same idea as the "OTE-AI"
bot: you reply on the thread, and it answers too.

How it connects (NO webhook / NO public URL needed):
  Uses Lark's *long-connection* (WebSocket) event mode via the official
  `lark-oapi` SDK. The bot opens an outbound connection to Lark and receives
  message events over it, so it can run on your laptop or the encoder server
  with no inbound ports or domain.

Flow:
  1. Lark pushes an `im.message.receive_v1` event over the long connection.
  2. We pull the text out of the message (stripping @mention placeholders).
  3. We add it to that thread's short history and call the OpenAI Chat API.
  4. We post the answer back as a threaded reply (reply_in_thread=True).

Reuses the SAME Lark Custom App as encoder_monitor.py (LARK_APP_ID /
LARK_APP_SECRET). Only one new secret is needed: OPENAI_API_KEY.

Dependencies: `lark-oapi` (see requirements.txt). The OpenAI call uses only
the Python standard library. Python 3.7+.

Run:
  source lark_ai_bot.env        # or: set -a; . lark_ai_bot.env; set +a
  python3 lark_ai_bot.py
"""

import os
import re
import sys
import json
import time
import urllib.request
import urllib.error
from collections import OrderedDict

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    P2ImMessageReceiveV1,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
    CreateMessageReactionRequest,
    CreateMessageReactionRequestBody,
    Emoji,
)

# Reuse the real encoder logic from the monitor so the bot can actually CHECK
# encoders (not just talk about them). These read ENCODER_* env at import time.
import encoder_monitor as enc

# ----------------------------------------------------------------------------
# CONFIG  (read from env so secrets never live in the repo)
# ----------------------------------------------------------------------------
# --- Lark Custom App (same app as encoder_monitor.py) ---
LARK_APP_ID     = os.environ.get("LARK_APP_ID", "")
LARK_APP_SECRET = os.environ.get("LARK_APP_SECRET", "")
# Which Lark cluster the app lives on. larksuite.com = Lark international (what
# encoder_monitor.env uses); feishu.cn = Feishu (China). MUST match your app, or
# the long connection fails with "Incorrect domain name".
LARK_DOMAIN     = os.environ.get("LARK_DOMAIN", "https://open.larksuite.com")

# --- OpenAI (the brain) ---
OPENAI_API_KEY  = os.environ.get("OPENAI_API_KEY", "")
OPENAI_BASE     = os.environ.get("OPENAI_BASE", "https://api.openai.com/v1")
OPENAI_MODEL    = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_TIMEOUT  = int(os.environ.get("OPENAI_TIMEOUT", "60"))
SYSTEM_PROMPT   = os.environ.get(
    "SYSTEM_PROMPT",
    "You are the OTE encoder assistant in a Lark chat. You help monitor TRTC "
    "video encoders. Each encoder is reachable at http://<ip> and exposes its "
    "streaming config. When a user asks you to check, curl, test, or look at an "
    "encoder (or just gives you an IP and asks about it), CALL the check_encoder "
    "tool with that IP instead of explaining how to do it yourself — actually run "
    "it and report the result. If the user asks to re-check 'the unreachable "
    "encoders' (or the ones that failed) WITHOUT giving specific IPs, FIRST call "
    "list_unreachable to get the IPs from the latest monitor run, THEN call "
    "check_encoder for EVERY IP it returns and report each result separately — do "
    "not stop after the first. To answer questions about a specific encoder's "
    "config (usersig, RoomID, SDKAppID, UserID, PrivateMapKey, Agora/TRTC URL) "
    "when the user names it by CODE (e.g. ELV01_PC) rather than IP, FIRST call "
    "find_encoder to get its IP, THEN call check_encoder on that IP. Then report "
    "ONLY the specific field they asked for, as ONE short line — e.g. 'trtc' → just "
    "the TRTC push URL; 'usersig' → just the usersig; 'roomid' → just the RoomID. "
    "Use the Mainstream output (output 0) unless they explicitly name a substream. "
    "Do NOT dump the full config or all three streams unless the user explicitly "
    "asks for everything. Outputs: 0=Mainstream, 1=Substream 1, 2=Substream 2. "
    "Answer concisely in plain text.",
)

# how many past turns (user+assistant messages) to keep per thread for context
HISTORY_TURNS   = int(os.environ.get("HISTORY_TURNS", "12"))

# "commands" = ONLY the deterministic commands; anything else -> a help card.
# "llm"      = also allow free-form chat via the model (less reliable). Default
# is command-only to avoid the model hallucinating IPs / leaking tool syntax.
CHAT_MODE       = os.environ.get("CHAT_MODE", "commands")

# Written by encoder_monitor.py each run; the list_unreachable tool reads it so
# the bot can re-check "the unreachable encoders". Must match the monitor's STATE_FILE.
STATE_FILE      = os.environ.get(
    "STATE_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "last_run.json"))

# Lark re-delivers un-acked events (at-least-once), and on restart our in-memory
# de-dup is gone — so it can replay OLD messages. Ignore anything older than this
# many seconds: real-time chat is always fresh, anything older is a replay.
MAX_MSG_AGE_SEC = int(os.environ.get("MAX_MSG_AGE_SEC", "120"))

# emoji reactions the bot puts on YOUR message: one when it starts ("got it"),
# one when it's finished replying ("done"). Set REACTIONS=0 to turn off. The
# values are Lark emoji_type keys (e.g. OnIt, DONE, THUMBSUP, OK, DoneTick).
REACTIONS_ON    = os.environ.get("REACTIONS", "1").lower() not in ("0", "false", "no", "")
REACT_ACK       = os.environ.get("REACT_ACK", "THUMBSUP")  # on receive: "got it"
REACT_DONE      = os.environ.get("REACT_DONE", "DONE")     # after reply: "done"

# strips Lark's @mention placeholders like "@_user_1" / "@_all" from message text
MENTION_RE = re.compile(r"@_(?:user_\d+|all)\b")

# ----------------------------------------------------------------------------
# tiny per-thread memory + event de-dup (in-memory only; resets on restart)
# ----------------------------------------------------------------------------
# thread key -> [ {"role": "user"/"assistant", "content": "..."}, ... ]
_HISTORY = {}
# remember recently handled message_ids so Lark re-deliveries aren't answered twice
_SEEN = OrderedDict()
_SEEN_MAX = 2000

# one shared API client for sending replies (auto-manages the tenant token)
_client = (
    lark.Client.builder()
    .app_id(LARK_APP_ID)
    .app_secret(LARK_APP_SECRET)
    .domain(LARK_DOMAIN)
    .build()
)


def _already_seen(message_id):
    """True if we've handled this message_id before (idempotency guard)."""
    if message_id in _SEEN:
        return True
    _SEEN[message_id] = True
    while len(_SEEN) > _SEEN_MAX:
        _SEEN.popitem(last=False)
    return False


def _history_for(thread_key):
    return _HISTORY.setdefault(thread_key, [])


def _trim(history):
    """Keep only the most recent HISTORY_TURNS messages."""
    if len(history) > HISTORY_TURNS:
        del history[: len(history) - HISTORY_TURNS]


# ----------------------------------------------------------------------------
# Tools the LLM can actually call (function calling)
# ----------------------------------------------------------------------------
# JSON schema advertised to the model. When the user asks to check an encoder,
# the model returns a tool_call for this instead of replying with text.
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "check_encoder",
            "description": (
                "Curl a TRTC video encoder by IP and report whether it is reachable "
                "and its per-output streaming config (RoomID/UserID/SDKAppID/Usersig/"
                "PrivateMapKey and any Agora rtmp link). Use this whenever the user "
                "asks to check, curl, test, ping, or inspect an encoder, or gives an "
                "IP and asks about its config/status."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ip": {
                        "type": "string",
                        "description": "The encoder's IPv4 address, e.g. 10.230.84.78",
                    },
                    "outputs": {
                        "type": "string",
                        "description": (
                            "Comma-separated output indexes to check "
                            "(0=Mainstream, 1=Substream 1, 2=Substream 2). "
                            "Default '0,1,2'."
                        ),
                    },
                },
                "required": ["ip"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_unreachable",
            "description": (
                "Return the encoders that were unreachable (or had no TRTC config) "
                "in the most recent monitor run, with their table code and IP. Use "
                "this FIRST when the user asks to re-check / curl 'the unreachable "
                "encoders' (or 'the ones that failed') without giving specific IPs — "
                "then call check_encoder for each IP it returns."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_encoder",
            "description": (
                "Look up an encoder by its table code / name (e.g. 'ELV01_PC') and "
                "return its IP, studio and last-run status. Use this when the user "
                "names an encoder (not an IP) and you need its IP — e.g. to then "
                "call check_encoder on that IP to read config like usersig, RoomID, "
                "SDKAppID, UserID or PrivateMapKey."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string",
                             "description": "Encoder table code or partial name, e.g. ELV01_PC"},
                },
                "required": ["name"],
            },
        },
    },
]


def tool_check_encoder(ip, outputs="0,1,2"):
    """Actually curl the encoder (reusing encoder_monitor) and return a result dict.

    Returns JSON the model turns into a human answer — never raises; per-output
    errors are captured so one dead output doesn't fail the whole check.
    """
    ip = (ip or "").strip()
    m = enc.IPV4_RE.search(ip)
    if not m:
        return {"error": "not a valid IPv4 address: %r" % ip}
    ip = m.group(0)

    out_list = [o.strip() for o in str(outputs or "0,1,2").split(",") if o.strip()]
    label = {"0": "Mainstream", "1": "Substream 1", "2": "Substream 2"}
    streams, reachable = [], False
    for out in out_list:
        try:
            cfg = enc.parse_output_config(enc.fetch_output(ip, out))
            reachable = True
            streams.append(dict(output=out, stream=label.get(out, "output %s" % out),
                                has_trtc=bool(cfg.get("RoomID")), **cfg))
        except Exception as e:
            streams.append({"output": out, "stream": label.get(out, "output %s" % out),
                            "error": str(e)})
            # If we can't even reach the FIRST output, the box is down — stop here
            # instead of waiting out the timeout on every remaining output.
            if not reachable:
                return {"ip": ip, "reachable": False,
                        "error": "unreachable (%s)" % e, "streams": streams}
    return {"ip": ip, "reachable": reachable, "streams": streams}


def tool_list_unreachable():
    """Read the monitor's last-run state and return its unreachable / no-TRTC lists."""
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
    except Exception as e:
        return {"error": "no monitor results available yet (%s)" % e}
    return {"time": data.get("time"),
            "unreachable": data.get("unreachable", []),
            "no_trtc": data.get("no_trtc", [])}


def _load_state():
    """Read the monitor's last-run state file, or return None."""
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def tool_find_encoder(name=""):
    """Find encoders by table code, tolerant of RoomID-style queries.

    Ignores case and non-alphanumerics, and matches either direction so e.g.
    'ELV01_PC_MAIN' (a RoomID) finds table 'ELV01_PC'. Most specific (longest
    table) first.
    """
    data = _load_state()
    if data is None:
        return {"error": "no monitor results available yet — run the monitor first"}
    norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())
    q = norm(name)
    matches = [e for e in data.get("encoders", [])
               if q and (q == norm(e.get("table", "")) or norm(e.get("table", "")) in q
                         or q in norm(e.get("table", "")))]
    matches.sort(key=lambda e: len(e.get("table", "")), reverse=True)
    return {"query": name, "matches": matches,
            "hint": "call check_encoder on the best (first) match's ip for live config"}


_TOOL_DISPATCH = {
    "check_encoder": tool_check_encoder,
    "list_unreachable": tool_list_unreachable,
    "find_encoder": tool_find_encoder,
}


def _card(title, template, elements):
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": template, "title": {"tag": "plain_text", "content": title}},
        "elements": elements,
    }


def build_encoder_update_card():
    """Build an interactive card of the last run's recorded / not-recorded tables.

    Drives the /encoder-update command (deterministic — no LLM).
    """
    data = _load_state()
    if data is None:
        return _card("📋 Encoder Update", "orange", [{"tag": "div", "text": {"tag": "lark_md",
                "content": "⚠️ No monitor results yet — run the encoder monitor first."}}])
    encoders = data.get("encoders", [])
    now = data.get("time", "?")
    if not encoders:
        return _card("📋 Encoder Update", "orange", [{"tag": "div", "text": {"tag": "lark_md",
                "content": "No encoders found in the last run (%s)." % now}}])

    tab_names = {"pc": "ENCODER (PC)", "sdk": "ENCODER (SDK)-NEW",
                 "trtc": "TRTC", "agora": "Agora"}
    tab_order = ["pc", "sdk", "trtc", "agora"]
    status_txt = {"unreachable": "unreachable", "no_trtc": "reachable but no TRTC",
                  "no_labels": "template missing param labels"}

    # studio -> tab -> {ok, bad}
    studios = {}
    for e in encoders:
        tabs = studios.setdefault(e["studio"], {})
        g = tabs.setdefault(e.get("tab", ""), {"ok": [], "bad": []})
        (g["ok"] if e.get("status") == "ok" else g["bad"]).append(e)

    any_bad = any(g["bad"] for tabs in studios.values() for g in tabs.values())
    elements = [{"tag": "div", "text": {"tag": "lark_md", "content": "🕒 last run %s" % now}}]
    for studio, tabs in studios.items():
        elements.append({"tag": "hr"})
        lines = ["<font color='blue'>**%s**</font>" % studio.upper()]
        ordered = [t for t in tab_order if t in tabs] + [t for t in tabs if t not in tab_order]
        for tab in ordered:
            g = tabs[tab]
            lines.append("▸ **%s**" % tab_names.get(tab, tab.upper()))
            lines.append("<font color='green'>✅ Recorded (%d)</font>" % len(g["ok"]))
            if g["ok"]:
                lines.append(", ".join("`%s`" % e["table"] for e in g["ok"]))
            if g["bad"]:
                lines.append("<font color='red'>❌ Not recorded (%d)</font>" % len(g["bad"]))
                for e in g["bad"]:
                    lines.append("`%s` (%s) — %s"
                                 % (e["table"], e["ip"], status_txt.get(e.get("status"), e.get("status", "?"))))
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})
    return _card("📋 Encoder Update", "orange" if any_bad else "green", elements)


# command keyword -> the config field returned by check_encoder's per-stream dict
FIELD_KEYS = {
    "agora": "AgoraRTMP", "trtc": "TRTCRTMP", "usersig": "Usersig",
    "privatemapkey": "PrivateMapKey", "userid": "UserID",
    "sdkappid": "SDKAppID", "roomid": "RoomID",
}


def _find_one_encoder(table_query):
    """Best (most specific) encoder match from the last run, or None."""
    data = _load_state()
    if not data:
        return None
    norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())
    q = norm(table_query)
    cands = [e for e in data.get("encoders", [])
             if q and (q == norm(e.get("table", "")) or norm(e.get("table", "")) in q
                       or q in norm(e.get("table", "")))]
    cands.sort(key=lambda e: len(e.get("table", "")), reverse=True)
    return cands[0] if cands else None


def build_field_card(field_kw, table_query):
    """`<field> <table>` command: curl the encoder live and show that field for
    Mainstream / Substream 1 / Substream 2. Deterministic — no LLM.
    """
    key = FIELD_KEYS[field_kw]
    enc = _find_one_encoder(table_query)
    if not enc:
        return _card("🔎 %s" % field_kw, "orange", [{"tag": "div", "text": {"tag": "lark_md",
            "content": "Couldn't find encoder `%s` in the last run. Run `encoder-update` "
                       "to refresh the list." % table_query}}])
    res = tool_check_encoder(enc["ip"])
    if not res.get("reachable"):
        return _card("🔎 %s — %s" % (field_kw, enc["table"]), "red", [{"tag": "div", "text": {"tag": "lark_md",
            "content": "🔴 `%s` (%s) is unreachable — can't read live config." % (enc["table"], enc["ip"])}}])
    room = res["streams"][0].get("RoomID", "") if res.get("streams") else ""
    lines = ["<font color='blue'>**%s — %s**</font>" % (field_kw.upper(), enc["table"]),
             "RoomID %s · %s" % (room or "?", enc["ip"])]
    for s in res.get("streams", []):
        lines.append("<font color='blue'>**%s** (UserID %s)</font>"
                     % (s.get("stream", "output %s" % s.get("output")), s.get("UserID") or "?"))
        lines.append(s.get(key) or "—")
    return _card("🔎 %s lookup" % field_kw, "green",
                 [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}])


def build_recheck_card():
    """Re-curl every encoder that was unreachable in the last run. Deterministic —
    uses the real IPs from last_run.json, never the LLM (no hallucinated IPs).
    """
    data = _load_state()
    if not data:
        return _card("🔁 Recheck", "orange", [{"tag": "div", "text": {"tag": "lark_md",
            "content": "No monitor results yet — run `encoder-update` first."}}])
    targets = data.get("unreachable", [])
    if not targets:
        return _card("🔁 Recheck unreachable", "green", [{"tag": "div", "text": {"tag": "lark_md",
            "content": "✅ No unreachable encoders in the last run (%s)." % data.get("time", "?")}}])

    now_ok, still = [], []
    for e in targets:
        (now_ok if tool_check_encoder(e["ip"]).get("reachable") else still).append(e)

    lines = []
    if now_ok:
        lines.append("<font color='green'>**✅ Now reachable (%d)**</font>" % len(now_ok))
        lines += ["`%s` (%s)" % (e["table"], e["ip"]) for e in now_ok]
    if still:
        lines.append("<font color='red'>**🔴 Still unreachable (%d)**</font>" % len(still))
        lines += ["`%s` (%s)" % (e["table"], e["ip"]) for e in still]
    return _card("🔁 Recheck unreachable", "green" if not still else "orange",
                 [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}])


TAB_NAMES = {"pc": "ENCODER (PC)", "sdk": "ENCODER (SDK)-NEW", "trtc": "TRTC", "agora": "Agora"}
STATUS_TXT = {"unreachable": "unreachable", "no_trtc": "reachable but no TRTC",
              "no_labels": "template missing param labels"}


def _find_encoder_in_tab(table_query, tab):
    """Best match for a table code restricted to one tab (pc/sdk)."""
    data = _load_state()
    if not data:
        return None
    norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())
    q = norm(table_query)
    cands = [e for e in data.get("encoders", [])
             if e.get("tab") == tab and q and
             (q == norm(e.get("table", "")) or norm(e.get("table", "")) in q or q in norm(e.get("table", "")))]
    cands.sort(key=lambda e: len(e.get("table", "")), reverse=True)
    return cands[0] if cands else None


def build_tab_list_card(tab):
    """Recorded / not-recorded for ONE tab (pc/sdk/trtc/agora), grouped by studio."""
    name = TAB_NAMES.get(tab, tab.upper())
    data = _load_state()
    if not data:
        return _card("📋 %s" % name, "orange", [{"tag": "div", "text": {"tag": "lark_md",
            "content": "No monitor results yet — run the monitor first."}}])
    studios = {}                                   # studio -> {ok:[str], bad:[str]}
    if tab in ("pc", "sdk"):
        for e in data.get("encoders", []):
            if e.get("tab") != tab:
                continue
            g = studios.setdefault(e["studio"], {"ok": [], "bad": []})
            if e.get("status") == "ok":
                g["ok"].append("`%s`" % e["table"])
            else:
                g["bad"].append("`%s` (%s) — %s" % (e["table"], e["ip"],
                                STATUS_TXT.get(e.get("status"), e.get("status", "?"))))
    else:                                          # trtc / agora flat tabs
        for f in data.get("flat", []):
            if f.get("tab", "").lower() != tab:
                continue
            g = studios.setdefault(f["studio"], {"ok": [], "bad": []})
            g["ok"] += ["`%s`" % c for c in f.get("filled", [])]
            g["bad"] += ["`%s`" % c for c in f.get("missing", [])]
    if not studios:
        return _card("📋 %s" % name, "orange", [{"tag": "div", "text": {"tag": "lark_md",
            "content": "No data for the %s tab in the last run." % name}}])
    any_bad = any(g["bad"] for g in studios.values())
    elements = [{"tag": "div", "text": {"tag": "lark_md",
                 "content": "🕒 last run %s · **%s**" % (data.get("time", "?"), name)}}]
    for studio, g in studios.items():
        elements.append({"tag": "hr"})
        lines = ["<font color='blue'>**%s**</font>" % studio.upper(),
                 "<font color='green'>✅ Recorded (%d)</font>" % len(g["ok"])]
        if g["ok"]:
            lines.append(", ".join(g["ok"]))
        if g["bad"]:
            lines.append("<font color='red'>❌ Not recorded (%d)</font>" % len(g["bad"]))
            lines += g["bad"]
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}})
    return _card("📋 %s — recorded" % name, "orange" if any_bad else "green", elements)


def build_block_card(tab, table_query):
    """Full block for one encoder on a pc/sdk tab: Agora + SDK TRTC
    (RoomID/UserID/SDKAppID/PrivateMapKey/Usersig) across all 3 streams. Live curl.
    """
    name = TAB_NAMES.get(tab, tab.upper())
    enc = _find_encoder_in_tab(table_query, tab)
    if not enc:
        return _card("🔎 %s" % name, "orange", [{"tag": "div", "text": {"tag": "lark_md",
            "content": "Couldn't find `%s` on the %s tab in the last run." % (table_query, name)}}])
    res = tool_check_encoder(enc["ip"])
    if not res.get("reachable"):
        return _card("🔎 %s — %s" % (name, enc["table"]), "red", [{"tag": "div", "text": {"tag": "lark_md",
            "content": "🔴 `%s` (%s) is unreachable." % (enc["table"], enc["ip"])}}])
    streams = res.get("streams", [])
    lines = ["<font color='blue'>**%s — %s** (%s)</font>" % (name, enc["table"], enc["ip"]),
             "<font color='green'>**Agora**</font>",
             (streams[0].get("AgoraRTMP") if streams else "") or "—",
             "<font color='green'>**SDK TRTC**</font>"]
    for label in ("RoomID", "UserID", "SDKAppID", "PrivateMapKey", "Usersig"):
        lines.append("**%s**" % label)
        for s in streams:
            lines.append("<font color='blue'>**%s:**</font> %s"
                         % (s.get("stream", "output %s" % s.get("output")), s.get(label) or "—"))
    return _card("🔎 %s block — %s" % (name, enc["table"]), "green",
                 [{"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}])


def build_help_card():
    """Shown when the message isn't a recognized command (command-only mode)."""
    fields = " · ".join("`%s`" % k for k in FIELD_KEYS)
    content = (
        "I'm not familiar with that request 🤔 — try one of these:\n\n"
        "• `update` — recorded ✅ / not recorded ❌ per studio & tab\n"
        "• `curl` — re-test the encoders that were unreachable\n"
        "• `pc` / `sdk` / `trtc` / `agora` — recorded list for that tab\n"
        "• `pc <table>` / `sdk <table>` — full block (Agora + SDK TRTC, all 3 streams)\n"
        "• `<field> <table>` — one field, e.g. `usersig ELV01_PC`\n"
        "    fields: %s" % fields
    )
    return _card("🤖 Available commands", "blue",
                 [{"tag": "div", "text": {"tag": "lark_md", "content": content}}])


# ----------------------------------------------------------------------------
# LLM Chat Completions (stdlib HTTP) — with a tool-calling loop
# ----------------------------------------------------------------------------
def _llm_request(messages, tools=None):
    """One POST to the (OpenAI-compatible) chat API; returns the parsed body."""
    url = OPENAI_BASE.rstrip("/") + "/chat/completions"
    payload = {"model": OPENAI_MODEL, "messages": messages}
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + OPENAI_API_KEY,
            "Content-Type": "application/json",
            # Some providers (e.g. Groq) sit behind Cloudflare, which blocks the
            # default "Python-urllib/x" agent with a 403 "error code: 1010".
            # A normal User-Agent gets us through.
            "User-Agent": "curlencoder-lark-bot/1.0",
            "Accept": "application/json",
        },
        method="POST",
    )
    # Retry transient failures: 429 (rate limit, common on Groq's free tier) and
    # 5xx. Linear backoff: 2s, 4s. Other errors raise immediately.
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=OPENAI_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                sys.stderr.write("[llm retry] HTTP %s, attempt %d\n" % (e.code, attempt + 1))
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError("LLM API HTTP %s: %s" % (e.code, detail[:500]))


def llm_chat(messages, on_tools=None):
    """Run the chat, executing any tool calls, until the model returns text.

    `messages` is mutated with the assistant/tool turns; pass a throwaway copy
    (so tool plumbing stays out of the persisted per-thread history).

    `on_tools(tool_calls)` is called for EACH round the model runs tools — used
    to post a "checking…" notice before each batch of (slow) checks, so every
    encoder it checks is announced, not just the first.
    """
    for _ in range(12):                      # cap tool round-trips
        body = _llm_request(messages, tools=TOOLS)
        msg = body["choices"][0]["message"]
        tool_calls = msg.get("tool_calls")
        if not tool_calls:
            return (msg.get("content") or "").strip()

        if on_tools:                         # announce this round's checks
            try:
                on_tools(tool_calls)
            except Exception as e:
                sys.stderr.write("[on_tools error] %s\n" % e)

        messages.append(msg)                 # the assistant turn that requested tools
        for tc in tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            handler = _TOOL_DISPATCH.get(name)
            if handler is None:
                result = {"error": "unknown tool: %s" % name}
            else:
                try:
                    result = handler(**args)
                except Exception as e:       # never let a tool crash the turn
                    result = {"error": "%s failed: %s" % (name, e)}
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id"),
                "content": json.dumps(result),
            })
    # ran out of tool rounds — force one final answer with tools disabled so we
    # summarise what we already gathered instead of bailing with a generic error
    try:
        body = _llm_request(messages, tools=None)
        final = (body["choices"][0]["message"].get("content") or "").strip()
        if final:
            return final
    except Exception as e:
        sys.stderr.write("[final summary error] %s\n" % e)
    return "I checked what I could but couldn't wrap it up cleanly — try asking about one encoder at a time."


# ----------------------------------------------------------------------------
# Lark reply (threaded, like OTE-AI)
# ----------------------------------------------------------------------------
def reply_in_thread(message_id, text):
    """Post `text` as a threaded reply under the given message."""
    request = (
        ReplyMessageRequest.builder()
        .message_id(message_id)
        .request_body(
            ReplyMessageRequestBody.builder()
            .content(json.dumps({"text": text}))
            .msg_type("text")
            .reply_in_thread(True)
            .build()
        )
        .build()
    )
    resp = _client.im.v1.message.reply(request)
    if not resp.success():
        sys.stderr.write(
            "[reply failed] code=%s msg=%s\n" % (resp.code, resp.msg)
        )


def reply_card_in_thread(message_id, card):
    """Post an interactive card as a threaded reply (renders lark_md / colors)."""
    request = (
        ReplyMessageRequest.builder()
        .message_id(message_id)
        .request_body(
            ReplyMessageRequestBody.builder()
            .content(json.dumps(card))
            .msg_type("interactive")
            .reply_in_thread(True)
            .build()
        )
        .build()
    )
    resp = _client.im.v1.message.reply(request)
    if not resp.success():
        sys.stderr.write("[card reply failed] code=%s msg=%s\n" % (resp.code, resp.msg))


def add_reaction(message_id, emoji_type):
    """React to a message with a Lark emoji (e.g. 'OnIt', 'DONE').

    Best-effort: a reaction failure must never break the actual reply, so all
    errors are swallowed (logged only).
    """
    if not (REACTIONS_ON and emoji_type):
        return
    try:
        request = (
            CreateMessageReactionRequest.builder()
            .message_id(message_id)
            .request_body(
                CreateMessageReactionRequestBody.builder()
                .reaction_type(Emoji.builder().emoji_type(emoji_type).build())
                .build()
            )
            .build()
        )
        resp = _client.im.v1.message_reaction.create(request)
        if not resp.success():
            sys.stderr.write(
                "[reaction '%s' failed] code=%s msg=%s\n"
                % (emoji_type, resp.code, resp.msg)
            )
    except Exception as e:
        sys.stderr.write("[reaction '%s' error] %s\n" % (emoji_type, e))


def _strip_tool_leak(text):
    """Remove leaked tool-call syntax some models emit as text (Groq llama):
    lines like 'Function=find_encoder>{...}', '<function>' tags, 'IP=...' lines.
    """
    kept = []
    for ln in (text or "").splitlines():
        s = ln.strip()
        if s.startswith("Function=") or s.startswith("<function") or s.startswith("</function"):
            continue
        if re.match(r"^IP\s*=\s*['\"]", s):
            continue
        kept.append(ln)
    out = "\n".join(kept).replace("<function>", "").replace("</function>", "")
    return out.strip()


def _extract_text(message):
    """Get clean user text from a text message, dropping @mention placeholders."""
    try:
        content = json.loads(message.content or "{}")
    except (ValueError, TypeError):
        return ""
    text = content.get("text", "") or ""
    return MENTION_RE.sub("", text).strip()


# ----------------------------------------------------------------------------
# event handler: a message arrived
# ----------------------------------------------------------------------------
def on_message(data: P2ImMessageReceiveV1) -> None:
    msg = data.event.message
    message_id = msg.message_id

    if _already_seen(message_id):
        return

    # drop stale/replayed events (create_time is Unix ms) so a restart doesn't
    # make the bot answer messages from an hour ago
    try:
        age = time.time() - int(msg.create_time) / 1000.0
    except (TypeError, ValueError):
        age = 0
    if age > MAX_MSG_AGE_SEC:
        sys.stderr.write("[skip stale] message_id=%s age=%.0fs\n" % (message_id, age))
        return

    if msg.message_type != "text":
        reply_in_thread(message_id, "I can only read text messages right now 🙂")
        return

    user_text = _extract_text(msg)
    if not user_text:
        return

    # ---- deterministic commands (no LLM, no guessing) ----
    # leading '/' optional. first word = command; last word = table code (so both
    # "usersig ELV01_PC" and "usersig of ELV01_PC" work).
    parts = user_text.strip().split()
    cmd = parts[0].lower().lstrip("/") if parts else ""
    if cmd in ("update", "encoder-update", "encoder_update", "encoderupdate"):
        add_reaction(message_id, REACT_ACK)
        reply_card_in_thread(message_id, build_encoder_update_card())
        add_reaction(message_id, REACT_DONE)
        return
    arg = parts[-1] if len(parts) >= 2 else None
    # tab commands: `pc`/`sdk`/`trtc`/`agora` alone -> recorded list for that tab;
    # `pc <table>`/`sdk <table>` -> full block; `trtc <room>`/`agora <table>` -> URL
    if cmd in ("pc", "sdk", "trtc", "agora"):
        add_reaction(message_id, REACT_ACK)
        if arg is None:
            card = build_tab_list_card(cmd)
        elif cmd in ("pc", "sdk"):
            card = build_block_card(cmd, arg)
        else:
            card = build_field_card(cmd, arg)
        reply_card_in_thread(message_id, card)
        add_reaction(message_id, REACT_DONE)
        return
    # single-field lookups: usersig/userid/sdkappid/privatemapkey/roomid <table>
    if cmd in FIELD_KEYS and arg:
        add_reaction(message_id, REACT_ACK)
        reply_card_in_thread(message_id, build_field_card(cmd, arg))
        add_reaction(message_id, REACT_DONE)
        return

    # re-check the unreachable encoders — explicit command only (so "why is X
    # unreachable?" still goes to normal chat). Deterministic: real IPs, no LLM.
    if cmd in ("curl", "recheck"):
        add_reaction(message_id, REACT_ACK)
        reply_in_thread(message_id, "On it — re-curling the unreachable encoders… 🔁")
        reply_card_in_thread(message_id, build_recheck_card())
        add_reaction(message_id, REACT_DONE)
        return

    # not a known command — in command-only mode, show the help card instead of
    # the (unreliable) LLM. Set CHAT_MODE=llm to allow free-form chat.
    if CHAT_MODE != "llm":
        add_reaction(message_id, REACT_ACK)
        reply_card_in_thread(message_id, build_help_card())
        add_reaction(message_id, REACT_DONE)
        return

    # "got it" — react the moment we pick the message up
    add_reaction(message_id, REACT_ACK)

    # group follow-ups share a thread_id; DMs fall back to the chat_id
    thread_key = msg.thread_id or msg.chat_id
    history = _history_for(thread_key)
    history.append({"role": "user", "content": user_text})
    _trim(history)

    # posted once if/when the model decides to run a tool, so the user gets an
    # instant "on it, checking…" before the (slower) actual work + final answer.
    announced = set()                        # IPs already announced for this message
    def announce(tool_calls):
        ips = []
        for tc in tool_calls:
            fn = tc.get("function", {})
            if fn.get("name") != "check_encoder":
                continue
            try:
                ip = json.loads(fn.get("arguments") or "{}").get("ip", "")
            except ValueError:
                ip = ""
            if ip and ip not in announced:   # don't re-announce a repeat check
                announced.add(ip)
                ips.append(ip)
        # only announce actual encoder checks; stay silent for quick lookups
        # like list_unreachable so the thread isn't cluttered
        if ips:
            reply_in_thread(message_id, "On it — checking %s now… 🔍" % ", ".join(ips))

    # throwaway copy so tool-call / tool-result turns don't pollute the saved
    # per-thread history (we only persist the user text + final answer)
    convo = [{"role": "system", "content": SYSTEM_PROMPT}] + [dict(m) for m in history]
    try:
        answer = llm_chat(convo, on_tools=announce)
    except Exception as e:
        sys.stderr.write("[llm error] %s\n" % e)
        reply_in_thread(message_id, "⚠️ Sorry, I couldn't reach the AI service just now.")
        return

    answer = _strip_tool_leak(answer) or answer   # drop leaked tool-call syntax
    history.append({"role": "assistant", "content": answer})
    _trim(history)
    reply_in_thread(message_id, answer)
    # "done" — react once the reply is posted
    add_reaction(message_id, REACT_DONE)


# ----------------------------------------------------------------------------
# main: open the long connection and listen forever
# ----------------------------------------------------------------------------
def main():
    missing = [n for n, v in (
        ("LARK_APP_ID", LARK_APP_ID),
        ("LARK_APP_SECRET", LARK_APP_SECRET),
        ("OPENAI_API_KEY", OPENAI_API_KEY),
    ) if not v]
    if missing:
        sys.exit("Missing env vars: %s (source lark_ai_bot.env)" % ", ".join(missing))

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_im_message_receive_v1(on_message)
        .build()
    )

    print("Lark AI bot starting (long-connection)… model=%s" % OPENAI_MODEL)
    ws = lark.ws.Client(
        LARK_APP_ID,
        LARK_APP_SECRET,
        event_handler=handler,
        domain=LARK_DOMAIN,
        log_level=lark.LogLevel.INFO,
    )
    ws.start()   # blocks; reconnects automatically


if __name__ == "__main__":
    main()
