#!/usr/bin/env python3
"""
encoder_monitor.py
==================
Weekly cron job (runs Mon 07:00 on OTG-Prod).

Flow:
  (runs for EACH sheet listed in LARK_SHEETS — e.g. lavie/stots/dheights/newport)
  1. read the encoder template from a Lark Sheet — each encoder is a block:
        table | ip | Agora | -            | <main agora url> | - | -
              |    | QAT   | -            | -                | - | -
              |    | SDK TRTC | RoomID        | <Mainstream> | <Substream 1> | <Substream 2>
              |    |          | UserID        |   ...
              |    |          | SDKAppID      |   ...
              |    |          | PrivateMapKey |   ...
              |    |          | Usersig       |   ...
  2. for EACH encoder (found by its IP in column B), for each output/stream
     curl http://<ip>/get_output?input=0&output=N   (N = 0/1/2)
  3. pull from each output:
     - SDK TRTC: RoomID / UserID / SDKAppID / Usersig / PrivateMapKey
     - Agora: the rtmp push link (the rtmp_publish_uri pointing at an agora host)
     (output 0 -> Mainstream, 1 -> Substream 1, 2 -> Substream 2)
  4. write the SDK TRTC values into the block's E/F/G rows, and the Agora link
     into the E/F/G of the "Agora" row
  5. notify the OTE group chat with an interactive card (✅ / ⚠️), naming any
     encoder (table code + IP) that was unreachable or had no TRTC config

One unreachable encoder doesn't stop the run. Uses ONLY the Python standard
library so nothing needs pip on the prod server. Python 3.6+.

Config is read from environment variables (see CONFIG). Put them in
/opt/curlencoder/encoder_monitor.env and source it.
"""

import os
import re
import sys
import json
import ssl
import traceback
import datetime
import urllib.request
import urllib.parse
import urllib.error
import xml.etree.ElementTree as ET

# ----------------------------------------------------------------------------
# CONFIG  (read from env so secrets never live in the repo)
# ----------------------------------------------------------------------------
# --- Encoder access (applies to every encoder we curl) ---
# curl -s --digest -u admin:admin --connect-timeout 10 http://<ip>/get_status
ENCODER_SCHEME   = os.environ.get("ENCODER_SCHEME", "http")       # http or https
ENCODER_USER     = os.environ.get("ENCODER_USER", "admin")        # digest auth user
ENCODER_PASS     = os.environ.get("ENCODER_PASS", "admin")        # digest auth password
ENCODER_TIMEOUT  = int(os.environ.get("ENCODER_TIMEOUT", "10"))   # --connect-timeout 10
ENCODER_INPUT    = os.environ.get("ENCODER_INPUT", "0")           # /get_output?input=N (single HDMI = 0)

# --- Lark Custom App ---
LARK_DOMAIN      = os.environ.get("LARK_DOMAIN", "https://open.larksuite.com")  # open.feishu.cn for Feishu
LARK_APP_ID      = os.environ.get("LARK_APP_ID", "")
LARK_APP_SECRET  = os.environ.get("LARK_APP_SECRET", "")
LARK_CHAT_ID     = os.environ.get("LARK_CHAT_ID", "")             # OTE group chat_id (oc_xxx)

# --- The template Lark Sheet(s) we read AND write back into ---
# One run can fill several sheets. List them in LARK_SHEETS, space/comma separated,
# each entry is:   name=<token>@<tab_id>   (name and @tab are optional)
#   - <token>   = the long token in the sheet URL .../sheets/<token>
#   - <tab_id>  = the ?sheet=XXXX in the URL; omit it to use the sheet's first tab
# e.g. LARK_SHEETS="lavie=GYv...@0kuxDh stots=MSq... dheights=V03... newport=BH6..."
LARK_SHEETS      = os.environ.get("LARK_SHEETS", "")
# Backwards-compatible single-sheet fallback (used only if LARK_SHEETS is empty):
LARK_SHEET_TOKEN = os.environ.get("LARK_OUT_SHEET_TOKEN", "")
LARK_SHEET_TAB   = os.environ.get("LARK_OUT_SHEET_ID", "")
# Column layout of the template (Baccarat.xlsx style). Change if your sheet differs.
TPL_TABLE_COL    = os.environ.get("TPL_TABLE_COL", "A")           # column with the table/encoder name
TPL_IP_COL       = os.environ.get("TPL_IP_COL", "B")              # column with each encoder's IP
TPL_REMARK_COL   = os.environ.get("TPL_REMARK_COL", "C")          # column with Agora/QAT/SDK TRTC labels
TPL_PARAM_COL    = os.environ.get("TPL_PARAM_COL", "D")           # column with RoomID/UserID/... labels
TPL_STREAM_COLS  = os.environ.get("TPL_STREAM_COLS", "E,F,G")     # Mainstream, Substream 1, Substream 2
# encoder outputs that map to the stream columns above, in the SAME order.
# output 0 = Mainstream, 1 = Substream 1, 2 = Substream 2 (matches Baccarat.xlsx).
TPL_OUTPUTS      = os.environ.get("TPL_OUTPUTS", "0,1,2")

REQUEST_TIMEOUT  = int(os.environ.get("REQUEST_TIMEOUT", "30"))

# matches IPv4 addresses anywhere in a cell's text
IPV4_RE = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")

# parameter label (in TPL_PARAM_COL) -> field key from parse_trtc_streams().
# keys are normalised (lowercased, spaces removed) before matching.
PARAM_TO_FIELD = {
    "roomid":        "RoomID",
    "userid":        "UserID",
    "sdkappid":      "SDKAppID",
    "privatemapkey": "PrivateMapKey",
    "usersig":       "Usersig",
}


# ----------------------------------------------------------------------------
# small HTTP helpers (stdlib only)
# ----------------------------------------------------------------------------
def _http(method, url, headers=None, data=None, timeout=REQUEST_TIMEOUT):
    """Return (status_code, body_text). Raises on network error."""
    headers = headers or {}
    body = None
    if data is not None:
        body = data if isinstance(data, bytes) else data.encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.getcode(), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def http_get(url, headers=None):
    return _http("GET", url, headers=headers)


def http_post_json(url, payload, headers=None):
    headers = dict(headers or {})
    headers["Content-Type"] = "application/json; charset=utf-8"
    return _http("POST", url, headers=headers, data=json.dumps(payload))


def _col_to_idx(letter):
    """'A' -> 0, 'B' -> 1, ... 'AA' -> 26."""
    idx = 0
    for ch in letter.strip().upper():
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx - 1


# ----------------------------------------------------------------------------
# 1. curl an encoder's per-output config
# ----------------------------------------------------------------------------
def fetch_output(ip, output):
    """curl --digest http://<ip>/get_output?input=0&output=<N>  -> raw XML.

    This config endpoint holds the TRTC creds for every encoder (whether it
    pushes via rtmp-relay or connects to TRTC directly), unlike /get_status.
    """
    url = "%s://%s/get_output?input=%s&output=%s" % (ENCODER_SCHEME, ip, ENCODER_INPUT, output)
    pwd_mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
    pwd_mgr.add_password(None, url, ENCODER_USER, ENCODER_PASS)
    handler = urllib.request.HTTPDigestAuthHandler(pwd_mgr)
    opener = urllib.request.build_opener(handler)
    req = urllib.request.Request(url, headers={"Accept": "application/xml"})
    try:
        with opener.open(req, timeout=ENCODER_TIMEOUT) as resp:
            if resp.getcode() != 200:
                raise RuntimeError("Encoder HTTP %s" % resp.getcode())
            return resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise RuntimeError("Encoder request failed: HTTP %s" % e.code)


# ----------------------------------------------------------------------------
# 2. parse one output's TRTC config
# ----------------------------------------------------------------------------
def parse_output_config(xml_text):
    """Pull the SDK TRTC fields out of a /get_output response.

    Tag mapping (note PrivateMapKey is stored as 'room_password'):
      trtc_publish_room_id       -> RoomID
      trtc_publish_user_id       -> UserID
      trtc_publish_app_id        -> SDKAppID
      trtc_publish_user_sig      -> Usersig
      trtc_publish_room_password -> PrivateMapKey
    """
    root = ET.fromstring(xml_text)

    def tag(name):
        el = root.find(name)
        return (el.text or "").strip() if el is not None and el.text else ""

    # the Agora push link is whichever rtmp_publish_uri_N points at an agora host
    agora_rtmp = ""
    for i in range(3):
        uri = tag("rtmp_publish_uri_%d" % i)
        if "agora" in uri.lower():
            agora_rtmp = uri
            break

    return {
        "RoomID":        tag("trtc_publish_room_id"),
        "UserID":        tag("trtc_publish_user_id"),
        "SDKAppID":      tag("trtc_publish_app_id"),
        "Usersig":       tag("trtc_publish_user_sig"),
        "PrivateMapKey": tag("trtc_publish_room_password"),
        "AgoraRTMP":     agora_rtmp,
    }


# ----------------------------------------------------------------------------
# 3. Lark sheet: read template, fill values, plus auth + chat
# ----------------------------------------------------------------------------
def lark_token():
    url = LARK_DOMAIN + "/open-apis/auth/v3/tenant_access_token/internal"
    _, body = http_post_json(url, {"app_id": LARK_APP_ID, "app_secret": LARK_APP_SECRET})
    data = json.loads(body)
    if data.get("code") != 0:
        raise RuntimeError("Lark auth failed: %s" % body)
    return data["tenant_access_token"]


def _cell_text(cell):
    """A Lark cell can be a string, number, or a list of rich-text segments."""
    if cell is None:
        return ""
    if isinstance(cell, list):
        parts = []
        for seg in cell:
            parts.append(str(seg.get("text", "")) if isinstance(seg, dict) else str(seg))
        return " ".join(parts)
    return str(cell)


def lark_first_tab(token, sheet_token):
    """Return the first tab's sheet_id (used when a sheet URL has no ?sheet=XXXX)."""
    url = "%s/open-apis/sheets/v3/spreadsheets/%s/sheets/query" % (LARK_DOMAIN, sheet_token)
    _, body = http_get(url, headers={"Authorization": "Bearer " + token})
    data = json.loads(body)
    if data.get("code") != 0:
        raise RuntimeError("Lark sheet metadata failed: %s" % body)
    sheets = data.get("data", {}).get("sheets", [])
    if not sheets:
        raise RuntimeError("Spreadsheet %s has no tabs" % sheet_token)
    return sheets[0]["sheet_id"]


def lark_read_grid(token, sheet_token, tab, rng):
    """Read a range like 'tabid!A1:Z2000' and return the raw 2D values array."""
    full = "%s!%s" % (tab, rng)
    url = "%s/open-apis/sheets/v2/spreadsheets/%s/values/%s" % (
        LARK_DOMAIN, sheet_token, urllib.parse.quote(full, safe="!"))
    _, body = http_get(url, headers={"Authorization": "Bearer " + token})
    data = json.loads(body)
    if data.get("code") != 0:
        raise RuntimeError("Lark sheet read failed: %s" % body)
    return data.get("data", {}).get("valueRange", {}).get("values", []) or []


def read_template_blocks(token, sheet_token, tab):
    """Scan a template tab and return an ordered list of encoder blocks:

        [{"ip": "10.144.2.51", "rows": {"roomid": 4, "userid": 5, ...}}, ...]

    'rows' maps each normalised parameter label to its 1-indexed sheet row.
    The IP (column TPL_IP_COL) marks the start of a block; the SDK TRTC parameter
    rows below it belong to that block until the next IP appears.
    """
    grid = lark_read_grid(token, sheet_token, tab, "A1:Z2000")
    table_i = _col_to_idx(TPL_TABLE_COL)
    ip_i = _col_to_idx(TPL_IP_COL)
    remark_i = _col_to_idx(TPL_REMARK_COL)
    param_i = _col_to_idx(TPL_PARAM_COL)

    blocks = []
    current = None
    for r, row in enumerate(grid):           # r is 0-indexed; sheet row = r + 1
        ip_cell = _cell_text(row[ip_i]) if len(row) > ip_i else ""
        m = IPV4_RE.search(ip_cell)
        if m:
            table = _cell_text(row[table_i]).strip() if len(row) > table_i else ""
            current = {"table": table, "ip": m.group(0), "rows": {}, "agora_row": None}
            blocks.append(current)
        if current is None:
            continue
        remark = _cell_text(row[remark_i]).strip().lower() if len(row) > remark_i else ""
        if remark == "agora":
            current["agora_row"] = r + 1     # row whose E/F/G hold the Agora links
        param = _cell_text(row[param_i]).strip().lower().replace(" ", "") if len(row) > param_i else ""
        if param in PARAM_TO_FIELD:
            current["rows"][param] = r + 1
    return blocks


def lark_batch_write(token, sheet_token, value_ranges):
    """Write many cell ranges in one shot via the sheets batch-update API."""
    if not value_ranges:
        return
    url = "%s/open-apis/sheets/v2/spreadsheets/%s/values_batch_update" % (
        LARK_DOMAIN, sheet_token)
    headers = {"Authorization": "Bearer " + token}
    for i in range(0, len(value_ranges), 100):   # chunk to stay well under API limits
        chunk = value_ranges[i:i + 100]
        _, body = http_post_json(url, {"valueRanges": chunk}, headers=headers)
        data = json.loads(body)
        if data.get("code") != 0:
            raise RuntimeError("Lark batch write failed: %s" % body)


def build_value_ranges(block, streams, tab):
    """For one encoder block, build the cell ranges that fill its SDK TRTC rows.

    streams[0] -> Mainstream, streams[1] -> Substream 1, streams[2] -> Substream 2.
    """
    stream_cols = [c.strip() for c in TPL_STREAM_COLS.split(",") if c.strip()]
    first_col, last_col = stream_cols[0], stream_cols[-1]

    def row_range(row_num, field):
        vals = [streams[i].get(field, "") if i < len(streams) else "" for i in range(len(stream_cols))]
        rng = "%s!%s%d:%s%d" % (tab, first_col, row_num, last_col, row_num)
        return {"range": rng, "values": [vals]}

    ranges = []
    # SDK TRTC rows (RoomID / UserID / SDKAppID / PrivateMapKey / Usersig)
    for param, field in PARAM_TO_FIELD.items():
        row_num = block["rows"].get(param)
        if row_num:
            ranges.append(row_range(row_num, field))
    # Agora row: the per-output Agora rtmp link across Mainstream/Sub1/Sub2
    if block.get("agora_row"):
        ranges.append(row_range(block["agora_row"], "AgoraRTMP"))
    return ranges


def lark_send_message(token, text):
    """Plain-text message (used as the failure fallback)."""
    _lark_post_message(token, "text", {"text": text})


def lark_send_card(token, card):
    """Interactive card message (the run summary)."""
    _lark_post_message(token, "interactive", card)


def _lark_post_message(token, msg_type, content):
    url = "%s/open-apis/im/v1/messages?receive_id_type=chat_id" % LARK_DOMAIN
    payload = {
        "receive_id": LARK_CHAT_ID,
        "msg_type": msg_type,
        "content": json.dumps(content),
    }
    headers = {"Authorization": "Bearer " + token}
    _, body = http_post_json(url, payload, headers=headers)
    data = json.loads(body)
    if data.get("code") != 0:
        raise RuntimeError("Lark message failed: %s" % body)


def _split_token_tab(s):
    """Pull (token, tab) from any form: token, token@tab, token?sheet=tab,
    or a full URL like https://.../sheets/<token>?sheet=<tab>."""
    tab = ""
    if "/sheets/" in s:                       # full URL pasted
        s = s.split("/sheets/", 1)[1]
    if "?" in s:                              # ...?sheet=<tab>
        s, query = s.split("?", 1)
        mq = re.search(r"sheet=([^&]+)", query)
        if mq:
            tab = mq.group(1)
    if "@" in s:                              # token@<tab>
        s, tab2 = s.split("@", 1)
        tab = tab or tab2
    return s.strip(), tab.strip()


def parse_sheets():
    """Parse LARK_SHEETS into [(name, token, tab)]; falls back to the single-sheet env."""
    entries = []
    for e in LARK_SHEETS.replace(",", " ").split():
        name = ""
        # optional "name=" prefix (short identifier, not part of a URL/token)
        if "=" in e:
            left, right = e.split("=", 1)
            if re.match(r"^[A-Za-z0-9_-]{1,20}$", left):
                name, e = left, right
        token, tab = _split_token_tab(e)
        if token:
            entries.append((name or token[:8], token, tab))
    if not entries and LARK_SHEET_TOKEN:
        entries.append(("sheet", LARK_SHEET_TOKEN, LARK_SHEET_TAB))
    return entries


def process_sheet(token, name, sheet_token, tab):
    """Fill one sheet: read its blocks, curl each encoder, write the values back.

    Returns a stats dict {name, total, ok, unreachable[], no_trtc[]}.
    """
    if not tab:
        tab = lark_first_tab(token, sheet_token)   # URL had no ?sheet= -> first tab
    blocks = read_template_blocks(token, sheet_token, tab)
    outputs = [o.strip() for o in TPL_OUTPUTS.split(",") if o.strip()]

    value_ranges = []
    ok = 0
    unreachable = []     # (table, ip) couldn't be curled
    no_trtc = []         # (table, ip) reachable but no TRTC configured
    for block in blocks:
        ip = block["ip"]
        who = (block.get("table", ""), ip)
        streams = []
        reachable = True
        for out in outputs:
            try:
                streams.append(parse_output_config(fetch_output(ip, out)))
            except Exception as e:
                reachable = False
                sys.stderr.write("[%s %s %s out=%s] %s\n" % (name, who[0], ip, out, e))
                break
        if not reachable:
            unreachable.append(who)
            continue
        if not any(s.get("RoomID") for s in streams):
            no_trtc.append(who)
            sys.stderr.write("[%s %s %s] reachable but no TRTC config\n" % (name, who[0], ip))
            continue
        ok += 1
        value_ranges.extend(build_value_ranges(block, streams, tab))

    lark_batch_write(token, sheet_token, value_ranges)
    print("[%s] filled %d/%d (%d unreachable, %d no-TRTC)"
          % (name, ok, len(blocks), len(unreachable), len(no_trtc)))
    return {"name": name, "total": len(blocks), "ok": ok,
            "unreachable": unreachable, "no_trtc": no_trtc}


def build_summary_card(now, results):
    """Build a Lark card summarising every sheet (results = list of stat dicts)."""
    any_issue = any(r.get("error") or r["unreachable"] or r["no_trtc"] for r in results)
    header_template = "orange" if any_issue else "green"
    title = "⚠️ Encoder Monitor" if any_issue else "✅ Encoder Monitor"

    def names(items):
        return ", ".join("%s `%s`" % (t or "?", ip) for t, ip in items)

    elements = [{"tag": "div", "text": {"tag": "lark_md", "content": "🕒 %s" % now}}]
    for r in results:
        elements.append({"tag": "hr"})
        if r.get("error"):
            content = "**%s** — ❌ %s" % (r["name"], r["error"])
        else:
            lines = ["**%s** — filled **%d / %d**" % (r["name"], r["ok"], r["total"])]
            if r["unreachable"]:
                lines.append("🔴 Unreachable (%d): %s" % (len(r["unreachable"]), names(r["unreachable"])))
            if r["no_trtc"]:
                lines.append("⚪ No TRTC (%d): %s" % (len(r["no_trtc"]), names(r["no_trtc"])))
            content = "\n".join(lines)
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": content}})

    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": header_template, "title": {"tag": "plain_text", "content": title}},
        "elements": elements,
    }


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def main():
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    token = None
    try:
        sheets = parse_sheets()
        if not sheets:
            raise RuntimeError("No sheets configured — set LARK_SHEETS (or LARK_OUT_SHEET_TOKEN)")

        token = lark_token()
        results = []
        for name, sheet_token, tab in sheets:
            try:
                results.append(process_sheet(token, name, sheet_token, tab))
            except Exception as e:
                # one bad sheet must not stop the others
                sys.stderr.write("[%s] sheet failed: %s\n" % (name, e))
                results.append({"name": name, "total": 0, "ok": 0,
                                "unreachable": [], "no_trtc": [], "error": str(e)})

        lark_send_card(token, build_summary_card(now, results))
        print("Done: processed %d sheet(s)" % len(results))

    except Exception:
        err = traceback.format_exc()
        sys.stderr.write(err)
        try:
            t = token or lark_token()
            lark_send_message(t, "❌ Encoder monitor FAILED (%s)\n%s" % (now, err.strip()[-1500:]))
        except Exception:
            sys.stderr.write("\n[!] could not send failure message to Lark\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
