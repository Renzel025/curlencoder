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
    "it and report the result. Outputs map to streams: 0=Mainstream, "
    "1=Substream 1, 2=Substream 2. Answer clearly and concisely in plain text.",
)

# how many past turns (user+assistant messages) to keep per thread for context
HISTORY_TURNS   = int(os.environ.get("HISTORY_TURNS", "12"))

# emoji reactions the bot puts on YOUR message: one when it starts ("got it"),
# one when it's finished replying ("done"). Set REACTIONS=0 to turn off. The
# values are Lark emoji_type keys (e.g. OnIt, DONE, THUMBSUP, OK, DoneTick).
REACTIONS_ON    = os.environ.get("REACTIONS", "1").lower() not in ("0", "false", "no", "")
REACT_ACK       = os.environ.get("REACT_ACK", "OnIt")    # on receive: "got it"
REACT_DONE      = os.environ.get("REACT_DONE", "DONE")   # after reply: "done"

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
    }
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


_TOOL_DISPATCH = {"check_encoder": tool_check_encoder}


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

    `on_tools(tool_calls)` is called once, the first time the model decides to
    run tools — used to post a "hold on, checking…" notice before the slow work.
    """
    notified = False
    for _ in range(5):                       # cap tool round-trips
        body = _llm_request(messages, tools=TOOLS)
        msg = body["choices"][0]["message"]
        tool_calls = msg.get("tool_calls")
        if not tool_calls:
            return (msg.get("content") or "").strip()

        if on_tools and not notified:        # tell the user we're on it (once)
            notified = True
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
    return "I tried a few steps but couldn't finish that — please try rephrasing."


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

    # "got it" — react the moment we pick the message up
    add_reaction(message_id, REACT_ACK)

    # group follow-ups share a thread_id; DMs fall back to the chat_id
    thread_key = msg.thread_id or msg.chat_id
    history = _history_for(thread_key)
    history.append({"role": "user", "content": user_text})
    _trim(history)

    # posted once if/when the model decides to run a tool, so the user gets an
    # instant "on it, checking…" before the (slower) actual work + final answer.
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
            if ip:
                ips.append(ip)
        if ips:
            reply_in_thread(message_id, "On it — checking %s now… 🔍" % ", ".join(ips))
        else:
            reply_in_thread(message_id, "On it — working on that now… 🔍")

    # throwaway copy so tool-call / tool-result turns don't pollute the saved
    # per-thread history (we only persist the user text + final answer)
    convo = [{"role": "system", "content": SYSTEM_PROMPT}] + [dict(m) for m in history]
    try:
        answer = llm_chat(convo, on_tools=announce)
    except Exception as e:
        sys.stderr.write("[llm error] %s\n" % e)
        reply_in_thread(message_id, "⚠️ Sorry, I couldn't reach the AI service just now.")
        return

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
