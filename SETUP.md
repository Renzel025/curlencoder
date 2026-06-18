# Encoder Monitor — setup on OTG-Prod

## 1. Create the files on the server (one directory: /opt/curlencoder)
On the server, make the directory and create each file, then paste in the
contents from your local copies.
```bash
sudo mkdir -p /opt/curlencoder

# paste the contents of encoder_monitor.py, then save (Ctrl-O, Enter, Ctrl-X)
sudo nano /opt/curlencoder/encoder_monitor.py

# paste the contents of encoder_monitor.env, then save
sudo nano /opt/curlencoder/encoder_monitor.env

# lock down permissions
sudo chmod 600 /opt/curlencoder/encoder_monitor.env   # secrets — root only
sudo chmod 755 /opt/curlencoder/encoder_monitor.py
```
(`nano` is easiest; `vi` works too. Just make sure you paste the WHOLE file.)

## 2. Create the Lark Custom App (one time)
1. Go to the Lark Developer Console → **Create Custom App**.
2. Copy **App ID** (`cli_...`) and **App Secret** → into `encoder_monitor.env`.
3. **Permissions / Scopes** — add and publish these:
   - `im:message` and `im:message:send_as_bot`  (post to the OTE group)
   - `sheets:spreadsheet`  (read the IP-list sheet AND write the results sheet)
   - `docx:document`  (only needed if you use the Lark Doc fallback output)
4. **Add the bot to the OTE group chat** (otherwise it can't post there).
5. Get the **chat_id**: in the group, or via the API `GET /open-apis/im/v1/chats`.
   It looks like `oc_xxxxxxxx`. → `LARK_CHAT_ID`.
6. Get the **document_id**: open the Lark Doc; it's the long token in the URL
   `.../docx/<document_id>`. → `LARK_DOC_ID`.
   Make sure the app (or a group it's in) has edit access to that doc.
7. **Encoder IP sheet (INPUT)**: the IPs are read from a Lark spreadsheet.
   - `LARK_IP_SHEET_TOKEN` = the token in the sheet URL `.../sheets/<token>`.
   - The sheet is laid out in game sections: the game name is in column A on the
     first row of each block only, and the rows below it (blank A) belong to the
     same game. The script carries the section name down and collects every IPv4
     in column C (`encoder ip`) while the section matches `LARK_IP_SECTION`
     (default `baccarat`).
   - Columns are configurable: `LARK_IP_LABEL_COL` (A), `LARK_IP_VALUE_COL` (C).
   - Give the app **read access** to that sheet.

8. **Results sheet (OUTPUT)**: parsed results are appended as rows.
   - `LARK_OUT_SHEET_TOKEN` = the destination spreadsheet token.
   - `LARK_OUT_SHEET_ID` = the tab id — the `?sheet=XXXX` value in the sheet URL.
   - Columns written: `Date | Encoder IP | Room | UserID | SDKAppID | Status | FLV`.
   - Give the app **edit access** to that sheet.
   - Leave `LARK_OUT_SHEET_TOKEN` blank to append to the Lark Doc instead.

## 3. Test by hand before scheduling
```bash
source /opt/curlencoder/encoder_monitor.env
python3 /opt/curlencoder/encoder_monitor.py
```
You should see `OK: N/M encoders reachable`, new rows in the output sheet, and a ✅ in the group.

## 4. Add the cron job  (weekly, Monday 07:00)
```bash
sudo crontab -e
```
Add:
```cron
0 7 * * 1 . /opt/curlencoder/encoder_monitor.env && /usr/bin/python3 /opt/curlencoder/encoder_monitor.py >> /opt/curlencoder/encoder_monitor.log 2>&1
```
- `0 7 * * 1` = 07:00 every Monday. Change the last field for a different day
  (`0`/`7` = Sun, `1` = Mon ... `6` = Sat).
- The `. /opt/curlencoder/encoder_monitor.env` sources the secrets first.
- Output + errors are appended to `/opt/curlencoder/encoder_monitor.log`.

## Notes
- Cron uses the server's local timezone — confirm with `timedatectl` it's the
  one you expect for "7am".
- The script sends a ❌ message to the group on ANY failure, so a silent log
  is not the only signal.
