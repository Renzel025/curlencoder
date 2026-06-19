# Encoder Monitor — setup on OTG-Prod

## 1. Get the files onto the server (via git)
```bash
sudo yum install -y git              # or: sudo apt install -y git
sudo git clone https://github.com/<your-username>/curlencoder.git /opt/curlencoder
cd /opt/curlencoder

# create the .env from the template and fill in secrets (it is NOT in git)
sudo cp encoder_monitor.env.example encoder_monitor.env
sudo vi encoder_monitor.env
sudo chmod 600 encoder_monitor.env   # secrets — root only
```
To update later: `cd /opt/curlencoder && sudo git pull`.

## 2. Create the Lark Custom App (one time)
1. Go to the Lark Developer Console → **Create Custom App**.
2. Copy **App ID** (`cli_...`) and **App Secret** → into `encoder_monitor.env`.
3. **Permissions / Scopes** — add and publish these:
   - `im:message` and `im:message:send_as_bot`  (post to the OTE group)
   - `sheets:spreadsheet`  (read the template sheet AND write values back)
4. **Add the bot to the OTE group chat** (otherwise it can't post there).
5. Get the **chat_id**: in the group, or via `GET /open-apis/im/v1/chats`.
   It looks like `oc_xxxxxxxx`. → `LARK_CHAT_ID`.
6. **Share the template sheet with the app** with **edit** access (the app reads
   the encoder list from it and writes the parsed values back).

## 3. The template sheet
The script reads AND writes one Lark Sheet, laid out like `Baccarat.xlsx` — one
block per encoder:
```
A=table  B=ip            C=remark    D=parameter      E=Mainstream  F=Substream 1  G=Substream 2
ELV01    10.230.30.106   Agora       -                <agora url>   -              -
                         QAT         -
                         SDK TRTC    RoomID           <filled>      <filled>       <filled>
                                     UserID           ...
                                     SDKAppID         ...
                                     PrivateMapKey    ...
                                     Usersig          ...
```
- `LARK_OUT_SHEET_TOKEN` = token in the sheet URL `.../sheets/<token>`.
- `LARK_OUT_SHEET_ID` = the `?sheet=XXXX` tab id in the URL.
- The script finds each encoder by its **IP (column B)**, curls it, parses the 3
  TRTC streams, and fills the 5 `SDK TRTC` rows across **E/F/G**
  (Mainstream / Substream 1 / Substream 2).
- If your columns differ, adjust `TPL_IP_COL` / `TPL_PARAM_COL` / `TPL_STREAM_COLS`.

## 4. Test by hand before scheduling
```bash
source /opt/curlencoder/encoder_monitor.env
python3 /opt/curlencoder/encoder_monitor.py
```
You should see `OK: filled N/M encoders`, the E/F/G cells populated in the sheet,
and a ✅ in the group.

## 5. Add the cron job  (weekly, Monday 07:00)
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
