#!/usr/bin/env python3
"""
encoder_monitor.py
==================
Weekly cron job (runs Mon 07:00 on OTG-Prod).

Flow:
  1. read the encoder template from a Lark Sheet — each encoder is a block:
        table | ip | Agora | -            | <main agora url> | - | -
              |    | QAT   | -            | -                | - | -
              |    | SDK TRTC | RoomID        | <Mainstream> | <Substream 1> | <Substream 2>
              |    |          | UserID        |   ...
              |    |          | SDKAppID      |   ...
              |    |          | PrivateMapKey |   ...
              |    |          | Usersig       |   ...
  2. for EACH encoder (found by its IP in column B) curl http://<ip>/get_status
  3. parse the 3 TRTC publish streams (Mainstream / Substream 1 / Substream 2),
     pulling RoomID / UserID / SDKAppID / PrivateMapKey / Usersig from each
  4. write those values into columns E/F/G of the block's SDK TRTC rows
  5. notify the OTE group chat (✅ / ⚠️ / ❌)

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
ENCODER_PATH     = os.environ.get("ENCODER_PATH", "/get_status")  # shared path on every encoder
ENCODER_SCHEME   = os.environ.get("ENCODER_SCHEME", "http")       # http or https
ENCODER_USER     = os.environ.get("ENCODER_USER", "admin")        # digest auth user
ENCODER_PASS     = os.environ.get("ENCODER_PASS", "admin")        # digest auth password
ENCODER_TIMEOUT  = int(os.environ.get("ENCODER_TIMEOUT", "10"))   # --connect-timeout 10

# --- Lark Custom App ---
LARK_DOMAIN      = os.environ.get("LARK_DOMAIN", "https://open.larksuite.com")  # open.feishu.cn for Feishu
LARK_APP_ID      = os.environ.get("LARK_APP_ID", "")
LARK_APP_SECRET  = os.environ.get("LARK_APP_SECRET", "")
LARK_CHAT_ID     = os.environ.get("LARK_CHAT_ID", "")             # OTE group chat_id (oc_xxx)

# --- The template Lark Sheet we read AND write back into ---
LARK_SHEET_TOKEN = os.environ.get("LARK_OUT_SHEET_TOKEN", "")     # spreadsheet token from the URL
LARK_SHEET_TAB   = os.environ.get("LARK_OUT_SHEET_ID", "")        # tab id (the ?sheet=XXXX in the URL)
# Column layout of the template (Baccarat.xlsx style). Change if your sheet differs.
TPL_IP_COL       = os.environ.get("TPL_IP_COL", "B")              # column with each encoder's IP
TPL_PARAM_COL    = os.environ.get("TPL_PARAM_COL", "D")           # column with RoomID/UserID/... labels
TPL_STREAM_COLS  = os.environ.get("TPL_STREAM_COLS", "E,F,G")     # Mainstream, Substream 1, Substream 2

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
# 1. curl an encoder
# ----------------------------------------------------------------------------
def fetch_encoder_xml(ip):
    """curl -s --digest -u admin:admin --connect-timeout 10 http://<ip>/get_status"""
    path = ENCODER_PATH if ENCODER_PATH.startswith("/") else "/" + ENCODER_PATH
    url = "%s://%s%s" % (ENCODER_SCHEME, ip, path)
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
# 2. parse the TRTC publish streams
# ----------------------------------------------------------------------------
def parse_trtc_streams(xml_text):
    """Return a list of TRTC stream dicts, in venc order (Mainstream, Sub1, Sub2...).

    Each <venc> block pushes to several destinations; we keep ONLY the TRTC one
    (rtc.qq.com/push/<room>?sdkappid=...&userid=...&usersig=...&private_map_key=...)
    and ignore the Agora (/live/<key>) URLs that have no query parameters.

    From the TRTC url we pull:
      RoomID = last path segment, plus UserID / SDKAppID / Usersig / PrivateMapKey
      from the query string.
    """
    root = ET.fromstring(xml_text)
    streams = []
    for venc in root.iter("venc"):
        trtc = None
        for child in venc:
            tag = child.tag or ""
            txt = (child.text or "").strip()
            if "rtmp_publish_url" in tag and ("rtc.qq.com" in txt or "sdkappid=" in txt):
                trtc = txt
                break
        if not trtc:
            continue
        parts = urllib.parse.urlsplit(trtc)
        q = urllib.parse.parse_qs(parts.query)
        streams.append({
            "RoomID":        parts.path.rsplit("/", 1)[-1],
            "UserID":        (q.get("userid") or [""])[0],
            "SDKAppID":      (q.get("sdkappid") or [""])[0],
            "PrivateMapKey": (q.get("private_map_key") or [""])[0],
            "Usersig":       (q.get("usersig") or [""])[0],
        })
    return streams


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


def lark_read_grid(token, rng):
    """Read a range like 'tabid!A1:Z2000' and return the raw 2D values array."""
    full = "%s!%s" % (LARK_SHEET_TAB, rng)
    url = "%s/open-apis/sheets/v2/spreadsheets/%s/values/%s" % (
        LARK_DOMAIN, LARK_SHEET_TOKEN, urllib.parse.quote(full, safe="!"))
    _, body = http_get(url, headers={"Authorization": "Bearer " + token})
    data = json.loads(body)
    if data.get("code") != 0:
        raise RuntimeError("Lark sheet read failed: %s" % body)
    return data.get("data", {}).get("valueRange", {}).get("values", []) or []


def read_template_blocks(token):
    """Scan the template tab and return an ordered list of encoder blocks:

        [{"ip": "10.144.2.51", "rows": {"roomid": 4, "userid": 5, ...}}, ...]

    'rows' maps each normalised parameter label to its 1-indexed sheet row.
    The IP (column TPL_IP_COL) marks the start of a block; the SDK TRTC parameter
    rows below it belong to that block until the next IP appears.
    """
    grid = lark_read_grid(token, "A1:Z2000")
    ip_i = _col_to_idx(TPL_IP_COL)
    param_i = _col_to_idx(TPL_PARAM_COL)

    blocks = []
    current = None
    for r, row in enumerate(grid):           # r is 0-indexed; sheet row = r + 1
        ip_cell = _cell_text(row[ip_i]) if len(row) > ip_i else ""
        m = IPV4_RE.search(ip_cell)
        if m:
            current = {"ip": m.group(0), "rows": {}}
            blocks.append(current)
        if current is None:
            continue
        param = _cell_text(row[param_i]).strip().lower().replace(" ", "") if len(row) > param_i else ""
        if param in PARAM_TO_FIELD:
            current["rows"][param] = r + 1
    return blocks


def lark_batch_write(token, value_ranges):
    """Write many cell ranges in one shot via the sheets batch-update API."""
    if not value_ranges:
        return
    url = "%s/open-apis/sheets/v2/spreadsheets/%s/values_batch_update" % (
        LARK_DOMAIN, LARK_SHEET_TOKEN)
    headers = {"Authorization": "Bearer " + token}
    for i in range(0, len(value_ranges), 100):   # chunk to stay well under API limits
        chunk = value_ranges[i:i + 100]
        _, body = http_post_json(url, {"valueRanges": chunk}, headers=headers)
        data = json.loads(body)
        if data.get("code") != 0:
            raise RuntimeError("Lark batch write failed: %s" % body)


def build_value_ranges(block, streams):
    """For one encoder block, build the cell ranges that fill its SDK TRTC rows.

    streams[0] -> Mainstream, streams[1] -> Substream 1, streams[2] -> Substream 2.
    """
    stream_cols = [c.strip() for c in TPL_STREAM_COLS.split(",") if c.strip()]
    first_col, last_col = stream_cols[0], stream_cols[-1]
    ranges = []
    for param, field in PARAM_TO_FIELD.items():
        row_num = block["rows"].get(param)
        if not row_num:
            continue
        vals = [streams[i][field] if i < len(streams) else "" for i in range(len(stream_cols))]
        rng = "%s!%s%d:%s%d" % (LARK_SHEET_TAB, first_col, row_num, last_col, row_num)
        ranges.append({"range": rng, "values": [vals]})
    return ranges


def lark_send_message(token, text):
    url = "%s/open-apis/im/v1/messages?receive_id_type=chat_id" % LARK_DOMAIN
    payload = {
        "receive_id": LARK_CHAT_ID,
        "msg_type": "text",
        "content": json.dumps({"text": text}),
    }
    headers = {"Authorization": "Bearer " + token}
    _, body = http_post_json(url, payload, headers=headers)
    data = json.loads(body)
    if data.get("code") != 0:
        raise RuntimeError("Lark message failed: %s" % body)


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def main():
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    token = None
    try:
        if not LARK_SHEET_TOKEN or not LARK_SHEET_TAB:
            raise RuntimeError("Set LARK_OUT_SHEET_TOKEN and LARK_OUT_SHEET_ID (the template sheet)")

        token = lark_token()
        blocks = read_template_blocks(token)
        if not blocks:
            raise RuntimeError("Found 0 encoder blocks in the template — check TPL_IP_COL / the tab id")
        print("Found %d encoder block(s) in the template" % len(blocks))

        value_ranges = []
        ok = 0
        unreachable = 0
        no_trtc = 0
        for block in blocks:
            ip = block["ip"]
            try:
                xml_text = fetch_encoder_xml(ip)
                streams = parse_trtc_streams(xml_text)
            except Exception as e:
                unreachable += 1
                sys.stderr.write("[%s] %s\n" % (ip, e))
                continue
            if not streams:
                no_trtc += 1
                sys.stderr.write("[%s] reachable but no TRTC stream found\n" % ip)
                continue
            ok += 1
            value_ranges.extend(build_value_ranges(block, streams))

        # write everything back to the sheet
        lark_batch_write(token, value_ranges)

        icon = "✅" if (unreachable == 0 and no_trtc == 0) else "⚠️"
        msg = ("%s Encoder monitor (%s)\n%d/%d encoders filled into the Lark Sheet."
               % (icon, now, ok, len(blocks)))
        if unreachable:
            msg += "\n%d unreachable." % unreachable
        if no_trtc:
            msg += "\n%d reachable but no TRTC stream." % no_trtc
        lark_send_message(token, msg)
        print("OK: filled %d/%d encoders (%d unreachable, %d no-TRTC)"
              % (ok, len(blocks), unreachable, no_trtc))

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
