#!/usr/bin/env python3
"""
encoder_monitor.py
==================
Weekly cron job (runs Mon 07:00 on OTG-Prod).

Flow:
  0. read the encoder IP list from a Lark Sheet (Baccarat section)
  1. curl EACH encoder endpoint  -> raw XML   (one bad host doesn't stop the run)
  2. parse the XML               -> RoomID / UserID / SDKAppID / ...
  3. push the data to a Lark Doc (append a dated section, one block per encoder)
  4. notify the OTE group chat:
        - ✅ summary if everything worked
        - ⚠️  summary if some encoders were unreachable
        - ❌ failure message if the run itself threw

Uses ONLY the Python standard library so nothing needs to be pip-installed
on the prod server. Python 3.6+.

Config is read from environment variables (see CONFIG below). Put them in
the crontab line or in /etc/encoder_monitor.env and source it.
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
# --- Encoder source ---
# curl -s --digest -u admin:admin --connect-timeout 10 http://10.170.2.195/get_status
# Many encoders: list IPs (comma/space/newline separated) in ENCODER_IPS and the
# script builds http://<ip><ENCODER_PATH> for each. ENCODER_URL still works as a
# single-host fallback for backwards compatibility.
ENCODER_IPS      = os.environ.get("ENCODER_IPS", "")             # "10.170.2.195,10.170.2.196,..."
ENCODER_PATH     = os.environ.get("ENCODER_PATH", "/get_status") # shared path on every encoder
ENCODER_SCHEME   = os.environ.get("ENCODER_SCHEME", "http")      # http or https
ENCODER_URL      = os.environ.get("ENCODER_URL", "")             # single-host fallback / legacy
ENCODER_USER     = os.environ.get("ENCODER_USER", "admin")     # digest auth user
ENCODER_PASS     = os.environ.get("ENCODER_PASS", "admin")     # digest auth password
ENCODER_TIMEOUT  = int(os.environ.get("ENCODER_TIMEOUT", "10"))  # --connect-timeout 10


def targets_from_ips(ips):
    """Turn a list of IPs into (label, url) pairs using the shared path/scheme."""
    path = ENCODER_PATH if ENCODER_PATH.startswith("/") else "/" + ENCODER_PATH
    return [(ip, "%s://%s%s" % (ENCODER_SCHEME, ip, path)) for ip in ips]


def encoder_targets_from_env():
    """Return (label, url) pairs from ENCODER_IPS / ENCODER_URL in the .env.

    Used only as a fallback when no Lark IP sheet is configured.
    """
    raw = ENCODER_IPS.replace(",", " ").replace("\n", " ")
    ips = [ip for ip in raw.split() if ip.strip()]
    targets = targets_from_ips(ips)
    if not targets and ENCODER_URL:
        # legacy single-host mode
        host = urllib.parse.urlsplit(ENCODER_URL).netloc or ENCODER_URL
        targets.append((host, ENCODER_URL))
    if not targets:
        raise RuntimeError("No encoders configured — set LARK_IP_SHEET_TOKEN, ENCODER_IPS, or ENCODER_URL")
    return targets

# --- Lark Custom App ---
LARK_DOMAIN      = os.environ.get("LARK_DOMAIN", "https://open.larksuite.com")  # use open.feishu.cn for Feishu
LARK_APP_ID      = os.environ.get("LARK_APP_ID", "")
LARK_APP_SECRET  = os.environ.get("LARK_APP_SECRET", "")
LARK_DOC_ID      = os.environ.get("LARK_DOC_ID", "")           # docx document_id (fallback output)
LARK_CHAT_ID     = os.environ.get("LARK_CHAT_ID", "")          # OTE group chat_id (oc_xxx)

# --- Output: Lark Sheet to write the parsed results into ---
# If LARK_OUT_SHEET_TOKEN is set, results are appended as rows to this sheet.
# Otherwise the script falls back to appending to the Lark Doc (LARK_DOC_ID).
LARK_OUT_SHEET_TOKEN = os.environ.get("LARK_OUT_SHEET_TOKEN", "")  # destination spreadsheet token
LARK_OUT_SHEET_ID    = os.environ.get("LARK_OUT_SHEET_ID", "")     # tab id (the ?sheet=XXXX in the URL)

# --- Lark Sheet that holds the encoder IP list ---
# The IPs are read from a Lark spreadsheet. We locate the "Baccarat" section
# (a cell in the label column) and pull every IPv4 found in the value column.
LARK_IP_SHEET_TOKEN = os.environ.get("LARK_IP_SHEET_TOKEN", "")   # spreadsheet token from the URL
LARK_IP_SHEET_NAME  = os.environ.get("LARK_IP_SHEET_NAME", "")    # tab name; blank = first sheet
LARK_IP_SECTION     = os.environ.get("LARK_IP_SECTION", "Baccarat")  # section label to match in the sheet
LARK_IP_LABEL_COL   = os.environ.get("LARK_IP_LABEL_COL", "A")    # column holding section names
LARK_IP_VALUE_COL   = os.environ.get("LARK_IP_VALUE_COL", "C")    # column holding the IP list

REQUEST_TIMEOUT  = int(os.environ.get("REQUEST_TIMEOUT", "30"))

# matches IPv4 addresses anywhere in a cell's text
IPV4_RE = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")


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
        # surface the error body so Lark/encoder error messages are visible
        return e.code, e.read().decode("utf-8", "replace")


def http_get(url, headers=None):
    return _http("GET", url, headers=headers)


def http_post_json(url, payload, headers=None):
    headers = dict(headers or {})
    headers["Content-Type"] = "application/json; charset=utf-8"
    return _http("POST", url, headers=headers, data=json.dumps(payload))


# ----------------------------------------------------------------------------
# 1. curl the encoders
# ----------------------------------------------------------------------------
def fetch_encoder_xml(url):
    """Equivalent of:
       curl -s --digest -u admin:admin --connect-timeout 10 <url>
    """
    if not url:
        raise RuntimeError("encoder url is empty")
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
        raise RuntimeError("Encoder request failed: HTTP %s\n%s"
                           % (e.code, e.read().decode("utf-8", "replace")[:500]))


# ----------------------------------------------------------------------------
# 2. parse the XML
# ----------------------------------------------------------------------------
def parse_encoders(xml_text):
    """
    Return a list of dicts, one row per published stream on the encoder.

    The real get_status XML looks like (one block per channel):

        <flv_url0>http://10.170.2.195/2.flv</flv_url0>
        <rtmp_publish_url_1>rtmp://intl-rtmp.rtc.qq.com/push/SDHST01_MAIN
            ?sdkappid=20021970&userid=SDHST01_360_MAIN_MAIN&usersig=...
            &private_map_key=...</rtmp_publish_url_1>

    The useful identifiers live INSIDE the rtmp publish URL:
      - stream/room name = last path segment   (SDHST01_MAIN)  -> RoomID
      - sdkappid query param                    (20021970)     -> SDKAppID
      - userid query param                      (SDHST01_...)   -> UserID
    FLV urls are matched to a stream by their trailing index when possible.
    """
    root = ET.fromstring(xml_text)

    # collect flv urls keyed by their trailing index: flv_url0 -> {0: url}
    flv_by_idx = {}
    rtmp_tags = []
    for el in root.iter():
        tag = el.tag or ""
        text = (el.text or "").strip()
        if not text:
            continue
        if "flv_url" in tag:
            m = re.search(r"(\d+)$", tag)
            flv_by_idx[m.group(1) if m else tag] = text
        elif "rtmp_publish_url" in tag or text.startswith("rtmp://"):
            rtmp_tags.append((tag, text))

    rows = []
    for tag, rtmp in rtmp_tags:
        idx_m = re.search(r"(\d+)$", tag)
        idx = idx_m.group(1) if idx_m else ""
        parts = urllib.parse.urlsplit(rtmp)
        q = urllib.parse.parse_qs(parts.query)
        stream = parts.path.rsplit("/", 1)[-1]  # SDHST01_MAIN
        rows.append({
            "RoomID":   stream,
            "UserID":   (q.get("userid") or [""])[0],
            "SDKAppID": (q.get("sdkappid") or [""])[0],
            "FLV":      flv_by_idx.get(idx, ""),
            "RTMP":     rtmp,
            "Status":   "publishing" if rtmp else "idle",
        })

    if not rows:
        raise RuntimeError("Parsed 0 streams — no rtmp_publish_url found in the XML")
    return rows


# ----------------------------------------------------------------------------
# 3. Lark: auth + doc append + chat message
# ----------------------------------------------------------------------------
def lark_token():
    url = LARK_DOMAIN + "/open-apis/auth/v3/tenant_access_token/internal"
    _, body = http_post_json(url, {"app_id": LARK_APP_ID, "app_secret": LARK_APP_SECRET})
    data = json.loads(body)
    if data.get("code") != 0:
        raise RuntimeError("Lark auth failed: %s" % body)
    return data["tenant_access_token"]


def _col_to_idx(letter):
    """'A' -> 0, 'B' -> 1, ... 'AA' -> 26."""
    idx = 0
    for ch in letter.strip().upper():
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx - 1


def lark_first_sheet_id(token):
    """Return the sheet_id of LARK_IP_SHEET_NAME, or the first tab if blank."""
    url = "%s/open-apis/sheets/v3/spreadsheets/%s/sheets/query" % (LARK_DOMAIN, LARK_IP_SHEET_TOKEN)
    _, body = http_get(url, headers={"Authorization": "Bearer " + token})
    data = json.loads(body)
    if data.get("code") != 0:
        raise RuntimeError("Lark sheet metadata failed: %s" % body)
    sheets = data.get("data", {}).get("sheets", [])
    if not sheets:
        raise RuntimeError("Spreadsheet has no sheets/tabs")
    if LARK_IP_SHEET_NAME:
        for s in sheets:
            if s.get("title") == LARK_IP_SHEET_NAME:
                return s["sheet_id"]
        raise RuntimeError("No tab named %r in the spreadsheet" % LARK_IP_SHEET_NAME)
    return sheets[0]["sheet_id"]


def lark_read_grid(token, sheet_id):
    """Read a wide range and return the raw 2D values array (rows of cells)."""
    rng = "%s!A1:Z500" % sheet_id
    url = "%s/open-apis/sheets/v2/spreadsheets/%s/values/%s" % (
        LARK_DOMAIN, LARK_IP_SHEET_TOKEN, urllib.parse.quote(rng, safe="!"))
    _, body = http_get(url, headers={"Authorization": "Bearer " + token})
    data = json.loads(body)
    if data.get("code") != 0:
        raise RuntimeError("Lark sheet read failed: %s" % body)
    return data.get("data", {}).get("valueRange", {}).get("values", []) or []


def _cell_text(cell):
    """A Lark cell can be a string, number, or a list of rich-text segments."""
    if cell is None:
        return ""
    if isinstance(cell, list):
        parts = []
        for seg in cell:
            if isinstance(seg, dict):
                parts.append(str(seg.get("text", "")))
            else:
                parts.append(str(seg))
        return " ".join(parts)
    return str(cell)


def lark_fetch_encoder_ips(token):
    """Read the IP-list spreadsheet and return the IPs for the Baccarat section.

    The sheet is laid out in game sections: the game name (column A) appears only
    on the FIRST row of each block; rows below it have a blank label and belong to
    the same game. So we carry the last seen section label DOWN the blank rows and
    collect every IPv4 in the VALUE column (C = "encoder ip") while the current
    section matches LARK_IP_SECTION ("baccarat"). All columns/section configurable.

    If no section ever matches, fall back to every IPv4 in the value column.
    """
    sheet_id = lark_first_sheet_id(token)
    grid = lark_read_grid(token, sheet_id)
    label_i = _col_to_idx(LARK_IP_LABEL_COL)
    value_i = _col_to_idx(LARK_IP_VALUE_COL)
    want = LARK_IP_SECTION.strip().lower()

    section_ips = []
    all_value_ips = []
    current = ""
    for row in grid:
        label = _cell_text(row[label_i]).strip() if len(row) > label_i else ""
        value = _cell_text(row[value_i]) if len(row) > value_i else ""
        if label:
            current = label          # new section header — carry it down blank rows
        found = IPV4_RE.findall(value)
        all_value_ips.extend(found)
        if want and want in current.lower():
            section_ips.extend(found)

    ips = section_ips or all_value_ips
    # de-dupe, keep order
    seen = set()
    uniq = []
    for ip in ips:
        if ip not in seen:
            seen.add(ip)
            uniq.append(ip)
    if not uniq:
        raise RuntimeError(
            "No IPs found in sheet %s (section=%r, value col=%s). "
            "Check LARK_IP_SECTION / LARK_IP_VALUE_COL." % (
                LARK_IP_SHEET_TOKEN, LARK_IP_SECTION, LARK_IP_VALUE_COL))
    return uniq


def lark_append_doc(token, lines):
    """
    Append a text block per line to the bottom of the docx document.
    block_id == document_id targets the document root.
    """
    url = "%s/open-apis/docx/v1/documents/%s/blocks/%s/children" % (
        LARK_DOMAIN, LARK_DOC_ID, LARK_DOC_ID)
    children = []
    for line in lines:
        children.append({
            "block_type": 2,  # text block
            "text": {"elements": [{"text_run": {"content": line}}]},
        })
    headers = {"Authorization": "Bearer " + token}
    _, body = http_post_json(url, {"children": children}, headers=headers)
    data = json.loads(body)
    if data.get("code") != 0:
        raise RuntimeError("Lark doc append failed: %s" % body)


# columns written to the output sheet, in order
SHEET_COLUMNS = ["Date", "Encoder IP", "Room", "UserID", "SDKAppID", "Status", "FLV"]


def lark_append_sheet(token, rows):
    """Write rows (list of lists, matching SHEET_COLUMNS) below existing data.

    Instead of the append API (which is picky about range height), we read how
    many rows the tab already has, then PUT an exact-size range right after them.
    A header row is written on the very first run (empty sheet).
    The tab is targeted by LARK_OUT_SHEET_ID (the ?sheet=XXXX value in the URL).
    """
    ncols = len(SHEET_COLUMNS)
    last_col = chr(ord("A") + ncols - 1)
    headers = {"Authorization": "Bearer " + token}

    # 1) find how many rows already have data in the tab
    read_rng = "%s!A1:%s5000" % (LARK_OUT_SHEET_ID, last_col)
    read_url = "%s/open-apis/sheets/v2/spreadsheets/%s/values/%s" % (
        LARK_DOMAIN, LARK_OUT_SHEET_TOKEN, urllib.parse.quote(read_rng, safe="!"))
    _, body = http_get(read_url, headers=headers)
    data = json.loads(body)
    if data.get("code") != 0:
        raise RuntimeError("Lark out-sheet read failed: %s" % body)
    existing = data.get("data", {}).get("valueRange", {}).get("values", []) or []
    # drop trailing empty rows so we start right after real data
    while existing and not any(c not in (None, "") for c in existing[-1]):
        existing.pop()
    start = len(existing) + 1

    # 2) header on first write
    values = ([SHEET_COLUMNS] + rows) if start == 1 else rows
    end = start + len(values) - 1

    # 3) write the exact range (dimensions match the values -> no range error)
    write_rng = "%s!A%d:%s%d" % (LARK_OUT_SHEET_ID, start, last_col, end)
    write_url = "%s/open-apis/sheets/v2/spreadsheets/%s/values" % (
        LARK_DOMAIN, LARK_OUT_SHEET_TOKEN)
    payload = {"valueRange": {"range": write_rng, "values": values}}
    hdr = dict(headers)
    hdr["Content-Type"] = "application/json; charset=utf-8"
    _, body = _http("PUT", write_url, headers=hdr, data=json.dumps(payload))
    data = json.loads(body)
    if data.get("code") != 0:
        raise RuntimeError("Lark sheet write failed: %s" % body)


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
        # 0: get the encoder IP list. Prefer the Lark sheet; fall back to .env.
        token = lark_token()
        if LARK_IP_SHEET_TOKEN:
            ips = lark_fetch_encoder_ips(token)
            targets = targets_from_ips(ips)
            print("Loaded %d encoder IP(s) from Lark sheet" % len(targets))
        else:
            targets = encoder_targets_from_env()

        # 1 + 2: curl + parse EVERY encoder. One bad host doesn't kill the run.
        sheet_rows = []      # rows for the output sheet (match SHEET_COLUMNS)
        doc_lines = ["=== Encoder report %s ===" % now]   # fallback doc output
        ok_count = 0
        fail_count = 0
        total_rows = 0
        for label, url in targets:
            doc_lines.append("")
            doc_lines.append("[%s]" % label)
            try:
                xml_text = fetch_encoder_xml(url)
                encoders = parse_encoders(xml_text)
            except Exception as e:
                fail_count += 1
                doc_lines.append("  ERROR: %s" % e)
                sheet_rows.append([now, label, "", "", "", "ERROR: %s" % e, ""])
                sys.stderr.write("[%s] %s\n" % (label, e))
                continue
            ok_count += 1
            total_rows += len(encoders)
            for enc in encoders:
                doc_lines.append(
                    "  Room=%s | UserID=%s | SDKAppID=%s | Status=%s | FLV=%s"
                    % (enc["RoomID"], enc["UserID"], enc["SDKAppID"],
                       enc["Status"], enc.get("FLV", ""))
                )
                sheet_rows.append([
                    now, label, enc["RoomID"], enc["UserID"],
                    enc["SDKAppID"], enc["Status"], enc.get("FLV", ""),
                ])

        # 3b: push to Lark — sheet if configured, otherwise the doc.
        if LARK_OUT_SHEET_TOKEN:
            lark_append_sheet(token, sheet_rows)
            dest = "the Lark Sheet"
        else:
            lark_append_doc(token, doc_lines)
            dest = "the Lark Doc"

        # 3c: summary notice to OTE group
        icon = "✅" if fail_count == 0 else "⚠️"
        lark_send_message(
            token,
            "%s Encoder monitor (%s)\n%d/%d encoders reachable, %d rows logged to %s.%s"
            % (
                icon, now, ok_count, len(targets), total_rows, dest,
                "" if fail_count == 0 else "\n%d encoder(s) FAILED — see %s." % (fail_count, dest),
            ),
        )
        print("OK: %d/%d encoders reachable, %d rows" % (ok_count, len(targets), total_rows))

    except Exception:
        err = traceback.format_exc()
        sys.stderr.write(err)
        # 3d: failure notice to OTE group (best effort)
        try:
            t = token or lark_token()
            lark_send_message(
                t,
                "❌ Encoder monitor FAILED (%s)\n%s" % (now, err.strip()[-1500:]),
            )
        except Exception:
            sys.stderr.write("\n[!] could not send failure message to Lark\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
