# Disposition store — MySQL + receiver on the LiveKit box

Puts the database and the five webhook endpoints on the same server as the bots. Nothing
leaves the machine: the bots POST over loopback, the receiver writes to MySQL on
loopback.

```
smarthealth-bot ─┐
roof-bot ────────┤
finance-bot ─────┼─ POST 127.0.0.1:8095/disposition/... ─→ MySQL 127.0.0.1:3306
movers-bot ──────┤
warranty-bot ────┘
```

The browser demo (`voicebot-web`) runs the same agents but posts nothing: its webhook is
blanked per call, so demo traffic never reaches these tables.

**Nothing about the existing services changes** except one line in each bot's `.env`
(step 7) — the URL they already post to.

---

## Ports on this box after this

| Port | Service |
|---|---|
| 8081 | smarthealth-bot (health) |
| 8082 | smarthealth-web (health) |
| 8083 | roof-bot (health) |
| 8084 | finance-bot (health) |
| 8085 | voicebot-web (health) |
| 8086 | movers-bot (health) |
| 8087 | warranty-bot (health) |
| 8090 | webdemo (loopback, behind Caddy) |
| **8095** | **disposition-receiver (loopback)** |
| **3306** | **MySQL (loopback)** |

---

## 1. Install MySQL

```bash
apt update && apt install -y mysql-server
systemctl enable --now mysql
systemctl status mysql --no-pager
```

Keep it modest — this box also runs LiveKit, three agent workers, the web demo and
Caddy on 4 vCPU / 8 GB:

```bash
cat > /etc/mysql/mysql.conf.d/zz-voicebot.cnf <<'EOF'
[mysqld]
# A few hundred rows a day; the default 128M pool is already generous. 256M gives
# headroom for reporting queries without competing with Silero VAD for RAM.
innodb_buffer_pool_size = 256M
max_connections         = 40

# Loopback only. The bots and the receiver are on this host. Read section 9 BEFORE
# changing this — the box has no host firewall, so 0.0.0.0 means the public internet.
bind-address            = 127.0.0.1
EOF

systemctl restart mysql
ss -lntp | grep 3306      # must show 127.0.0.1:3306, never 0.0.0.0:3306
```

Set a root password if the install didn't prompt:

```bash
mysql -e "ALTER USER 'root'@'localhost' IDENTIFIED WITH caching_sha2_password BY '8F8OyXbDXq8Dh3kp8eDdm';"
```

## 2. Create the database

Edit `CHANGE_ME_STRONG_PASSWORD` in `schema.sql` first, then:

```bash
mysql -u root -p < schema.sql

mysql -u root -p -e "USE voicebot; SHOW TABLES;"
# fin_call_dispositions
# mov_call_dispositions
# roof_call_dispositions
# sh_call_dispositions
# wty_call_dispositions
```

> On a server where `voicebot` **already exists**, do not re-run `schema.sql`.
> Campaigns C, D and E ship as migrations — `schema_finance.sql`, `schema_movers.sql`
> and `schema_warranty.sql` — each adding one table and touching nothing else. See step 3
> of `FINANCE_AU_DEPLOY.md`, `MOVERS_AU_DEPLOY.md` and `WARRANTY_UK_DEPLOY.md`.

## 3. Install the receiver

```powershell
# from your workstation
cd "C:\Users\hpal1\Downloads\python-headless-softphone-client\freelance-export\livekit-migration"
scp -r dispositions root@<DROPLET_IP>:/root/
```

```bash
mkdir -p /opt/disposition-receiver
cp /root/dispositions/{receiver.py,requirements.txt,schema.sql,DISPOSITION_SERVER_DEPLOY.md} /opt/disposition-receiver/
cp /root/dispositions/.env.example /opt/disposition-receiver/.env
cp /root/dispositions/disposition-receiver.service /etc/systemd/system/

python3 -m venv /opt/disposition-receiver/.venv
/opt/disposition-receiver/.venv/bin/pip install -q --upgrade pip
/opt/disposition-receiver/.venv/bin/pip install -q -r /opt/disposition-receiver/requirements.txt
systemctl daemon-reload
```

## 4. Configure

```bash
nano /opt/disposition-receiver/.env      # DB_PASSWORD = what you put in schema.sql
chmod 600 /opt/disposition-receiver/.env
```

Leave `WEBHOOK_TOKEN` blank unless you set the same value in **both** bots' `.env`
files. A token here that the bots don't send means every POST is rejected 401 and every
lead is lost.

## 5. Start and verify

```bash
systemctl enable --now disposition-receiver
curl -s localhost:8095/health          # {"ok":true,"db":"up"}
```

`/health` opens a real connection and runs `SELECT 1` — it fails if MySQL is down rather
than reporting healthy while leads are being dropped.

## 6. Test the endpoints BEFORE wiring the bots

Two are shown here. The others have their own test posts: finance one with an email
address and one without (`FINANCE_AU_DEPLOY.md` step 3), movers one with a quote and one
without (`MOVERS_AU_DEPLOY.md` step 3), warranty one quoted and one stopped by an
existing fault (`WARRANTY_UK_DEPLOY.md` step 3).

```bash
curl -s -X POST localhost:8095/disposition/roof \
  -H 'Content-Type: application/json' -d '{
  "call_id":"TEST-1","campaign":"roof-uk","phone":"+447700900123",
  "prospect_name":null,
  "started_at":"2026-09-02T10:14:02.113900+00:00",
  "ended_at":"2026-09-02T10:16:48.441200+00:00",
  "duration_sec":166,"outcome":"CALLBACK_AGREED",
  "outcome_note":"Adviser callback agreed - details captured",
  "consent_given":true,"callback_agreed":true,"callback_request":"",
  "decline_reason":"","remarks":"test row",
  "qualification":{"homeowner":"yes","interested":"yes","roof_age":"around 20 years",
    "checked_recently":"no","first_name":"Margaret","last_name":"",
    "address":"14 Elm Road, Bristol BS7 8QX","callback_number":"+447700900123"},
  "missing_required":[]}'
# {"status":"ok","id":1}

curl -s -X POST localhost:8095/disposition/smarthealth \
  -H 'Content-Type: application/json' -d '{
  "call_id":"TEST-2","agent":"smarthealth-bot","campaign":"test",
  "prospect":{"name":"Alex","phone":"+61400123456","state":null,"postcode":"3121"},
  "outcome":"QUALIFIED_PENDING",
  "disposition_code":"Connected - Qualified, advisor to call",
  "qualification":{"holds_policy":"yes","over_min_age":"yes","insurer":"BUPA",
    "cover_type":"family","premium_amount":"about three hundred and forty dollars",
    "premium_frequency":"monthly","hospital_extras":"both","permanent_resident":"",
    "main_interest":"reduce premiums","first_name":"Alex","last_name":"","email":"",
    "postcode":"3121","decision_maker":""},
  "consent_to_contact":true,"callback_request":"","remarks":"test row",
  "decline_reason":"",
  "started_at":"2026-09-01T05:51:38.060361+00:00",
  "ended_at":"2026-09-01T05:55:12.884210+00:00",
  "duration_seconds":214.8,
  "usage":{"stt_audio_duration":198.44,"llm_total_tokens":18734,"tts_characters_count":2891}}'
# {"status":"ok","id":1}
```

Confirm the rows landed with values in the right columns:

```bash
mysql -u root -p voicebot -e "
  SELECT id, call_id, outcome, qual_address, qual_callback_number FROM roof_call_dispositions\G
  SELECT id, call_id, outcome, qual_insurer, qual_premium_amount, started_at FROM sh_call_dispositions\G"
```

Check `started_at` reads `2026-09-01 05:51:38.060361` — offset stripped, UTC preserved.
Then delete the test rows:

```bash
mysql -u root -p voicebot -e "
  DELETE FROM roof_call_dispositions WHERE call_id='TEST-1';
  DELETE FROM sh_call_dispositions   WHERE call_id='TEST-2';"
```

## 7. Point the bots at it

```bash
sed -i 's|^DISPOSITION_WEBHOOK_URL=.*|DISPOSITION_WEBHOOK_URL=http://127.0.0.1:8095/disposition/smarthealth|' \
  /opt/livekit/bot/.env

sed -i 's|^DISPOSITION_WEBHOOK_URL=.*|DISPOSITION_WEBHOOK_URL=http://127.0.0.1:8095/disposition/roof|' \
  /opt/livekit/roof-bot/.env

sed -i 's|^DISPOSITION_WEBHOOK_URL=.*|DISPOSITION_WEBHOOK_URL=http://127.0.0.1:8095/disposition/finance|' \
  /opt/livekit/finance-bot/.env

sed -i 's|^DISPOSITION_WEBHOOK_URL=.*|DISPOSITION_WEBHOOK_URL=http://127.0.0.1:8095/disposition/movers|' \
  /opt/livekit/movers-bot/.env

sed -i 's|^DISPOSITION_WEBHOOK_URL=.*|DISPOSITION_WEBHOOK_URL=http://127.0.0.1:8095/disposition/warranty|' \
  /opt/livekit/warranty-bot/.env

grep DISPOSITION_WEBHOOK_URL /opt/livekit/bot/.env /opt/livekit/roof-bot/.env \
  /opt/livekit/finance-bot/.env /opt/livekit/movers-bot/.env /opt/livekit/warranty-bot/.env

systemctl restart smarthealth-bot roof-bot finance-bot movers-bot warranty-bot
```

If the line doesn't exist in a file, `sed` silently does nothing — check the `grep`
output before restarting.

## 8. End-to-end test

Place one real call on each campaign, watching both sides:

```bash
journalctl -u smarthealth-bot -u roof-bot -u finance-bot -u movers-bot -u warranty-bot \
  -u disposition-receiver -f -o cat
 | grep -Ei 'disposition|outcome|insert|error'
```

Expect `[smarthealth] disposition POST -> 200 {"status":"ok","id":N}` from the bot and a
matching line from the receiver. Then:

```bash
curl -s localhost:8095/stats
# {"smarthealth":{"QUALIFIED_PENDING":1}, "roof":{"CALLBACK_AGREED":1},
#  "finance":{"INFO_REQUESTED":1}, "movers":{"BOOKING_PENDING_DEPOSIT":1},
#  "warranty":{"INFO_REQUESTED":1}}
```

**The bot's local CSV keeps working regardless** — it is deliberately not removed. Two
independent copies of every lead is the right number while a new sink beds in.

---

## 9. Remote access for your developer

MySQL is bound to `127.0.0.1`. **This box has no host firewall** (`ufw` is inactive), so
binding to `0.0.0.0` publishes your leads database to the internet. Two safe options:

**SSH tunnel — nothing to open, recommended.** From the Vici box:

```bash
ssh -N -L 3306:127.0.0.1:3306 root@<DROPLET_IP>
# then connect to 127.0.0.1:3306 as usual
```

**Or open it to that host only.** Requires a DigitalOcean cloud firewall rule allowing
TCP 3306 **from the Vici IP alone** — set that up *first*, then:

```bash
sed -i 's/^bind-address.*/bind-address = 0.0.0.0/' /etc/mysql/mysql.conf.d/zz-voicebot.cnf
mysql -u root -p -e "
  CREATE USER 'voicebot_ro'@'<VICI_IP>' IDENTIFIED BY 'STRONG_PW';
  GRANT SELECT ON voicebot.* TO 'voicebot_ro'@'<VICI_IP>';"
systemctl restart mysql
ss -lntp | grep 3306
```

Read-only user, pinned to one host. Never `'voicebot_ro'@'%'` on an unfirewalled box.

## 10. Backups — do this before real traffic

This is now the system of record for every lead.

```bash
mkdir -p /var/backups/voicebot
cat > /etc/cron.daily/voicebot-backup <<'EOF'
#!/bin/sh
mysqldump --single-transaction --routines voicebot \
  | gzip > /var/backups/voicebot/voicebot-$(date +\%F).sql.gz
find /var/backups/voicebot -name '*.sql.gz' -mtime +30 -delete
EOF
chmod +x /etc/cron.daily/voicebot-backup
/etc/cron.daily/voicebot-backup && ls -lh /var/backups/voicebot
```

A backup on the same droplet protects against a bad query, not a dead droplet. Copy
them off-box (DO Spaces, or `rsync` to the Vici server) once volume justifies it.

## 11. When an insert fails

A failed insert returns 500 to the bot **and** appends the payload to
`/opt/disposition-receiver/failed_inserts.jsonl`. Nothing is lost silently.

```bash
wc -l /opt/disposition-receiver/failed_inserts.jsonl        # any lines = investigate
journalctl -u disposition-receiver -n 50 | grep FAILED
```

Replay them once the cause is fixed:

```bash
cd /opt/disposition-receiver
python3 - <<'EOF'
import json, urllib.request
ROUTE = {"sh_call_dispositions": "smarthealth", "roof_call_dispositions": "roof"}
done = []
for line in open("failed_inserts.jsonl", encoding="utf-8"):
    rec = json.loads(line)
    req = urllib.request.Request(
        f"http://127.0.0.1:8095/disposition/{ROUTE[rec['table']]}",
        data=json.dumps(rec["payload"]).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            print(r.status, rec["payload"].get("call_id"))
            done.append(line)
    except Exception as e:
        print("still failing:", e)
# only clear the lines that actually replayed
remaining = [l for l in open("failed_inserts.jsonl", encoding="utf-8") if l not in done]
open("failed_inserts.jsonl", "w", encoding="utf-8").writelines(remaining)
EOF
```

## 12. Rollback

```bash
systemctl disable --now disposition-receiver
# point the bots back at whatever URL they used before, then:
systemctl restart smarthealth-bot roof-bot
```

MySQL can stay installed; it costs ~250 MB of RAM and nothing else.
