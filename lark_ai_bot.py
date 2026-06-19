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
import urllib.request
import urllib.error
from collections import OrderedDict

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    P2ImMessageReceiveV1,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

# ----------------------------------------------------------------------------
# CONFIG  (read from env so secrets never live in the repo)
# ----------------------------------------------------------------------------
# --- Lark Custom App (same app as encoder_monitor.py) ---
LARK_APP_ID     = os.environ.get("LARK_APP_ID", "")
LARK_APP_SECRET = os.environ.get("LARK_APP_SECRET", "")

# --- OpenAI (the brain) ---
OPENAI_API_KEY  = os.environ.get("OPENAI_API_KEY", "")
OPENAI_BASE     = os.environ.get("OPENAI_BASE", "https://api.openai.com/v1")
OPENAI_MODEL    = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_TIMEOUT  = int(os.environ.get("OPENAI_TIMEOUT", "60"))
SYSTEM_PROMPT   = os.environ.get(
    "SYSTEM_PROMPT",
    "You are a helpful assistant in a Lark (Feishu) chat. Answer clearly and "
    "concisely. Use plain text — no markdown tables or images.",
)

# how many past turns (user+assistant messages) to keep per thread for context
HISTORY_TURNS   = int(os.environ.get("HISTORY_TURNS", "12"))

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
# OpenAI Chat Completions (stdlib HTTP)
# ----------------------------------------------------------------------------
def openai_chat(messages):
    """POST to the OpenAI Chat Completions API and return the reply text."""
    url = OPENAI_BASE.rstrip("/") + "/chat/completions"
    payload = {"model": OPENAI_MODEL, "messages": messages}
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": "Bearer " + OPENAI_API_KEY,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=OPENAI_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise RuntimeError("OpenAI HTTP %s: %s" % (e.code, detail[:500]))
    return body["choices"][0]["message"]["content"].strip()


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

    if msg.message_type != "text":
        reply_in_thread(message_id, "I can only read text messages right now 🙂")
        return

    user_text = _extract_text(msg)
    if not user_text:
        return

    # group follow-ups share a thread_id; DMs fall back to the chat_id
    thread_key = msg.thread_id or msg.chat_id
    history = _history_for(thread_key)
    history.append({"role": "user", "content": user_text})
    _trim(history)

    try:
        answer = openai_chat([{"role": "system", "content": SYSTEM_PROMPT}] + history)
    except Exception as e:
        sys.stderr.write("[openai error] %s\n" % e)
        reply_in_thread(message_id, "⚠️ Sorry, I couldn't reach the AI service just now.")
        return

    history.append({"role": "assistant", "content": answer})
    _trim(history)
    reply_in_thread(message_id, answer)


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
        log_level=lark.LogLevel.INFO,
    )
    ws.start()   # blocks; reconnects automatically


if __name__ == "__main__":
    main()
